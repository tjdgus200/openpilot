from pathlib import Path
import sys
import runpy
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import power_setup as power
from host_health import power_health


@pytest.fixture
def board(tmp_path, monkeypatch):
  files = {
    'proc/device-tree/model': 'NVIDIA Jetson Orin Nano Developer Kit\0',
    'proc/device-tree/compatible': 'nvidia,p3768-0000+p3767-0005-super\0nvidia,tegra234\0',
    'proc/sys/kernel/random/boot_id': 'boot1',
    'etc/nv_tegra_release': '# R36 (release), REVISION: 4.7',
    'etc/nvpmodel.conf': '< POWER_MODEL ID=0 NAME=15W >\n< POWER_MODEL NAME=MAXN_SUPER ID=7 >',
    'sys/class/devfreq/17000000.gpu/cur_freq': '1020000000',
    'sys/class/devfreq/17000000.gpu/max_freq': '1020000000',
  }
  for name, text in files.items():
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
  (tmp_path / 'run').mkdir()
  calls, mode = [], ['15W', 0]
  def command(*args):
    calls.append(args)
    if args[-1] == '-q':
      return NS(returncode=0, stdout=f'NV Power Mode: {mode[0]}\n{mode[1]}\n', stderr='')
    if '-m' in args:
      assert args[-1] == '7'
      mode[:] = ['MAXN_SUPER', 7]
    return NS(returncode=0, stdout='', stderr='')
  monkeypatch.setattr(power, 'command', command)
  return tmp_path, calls, mode


def test_selects_installed_profile_by_name_then_locks_clocks(board):
  root, calls, _ = board
  result = power.apply(root)
  assert result['state'] == 'ready'
  assert result['before'] == {'name': '15W', 'id': 0}
  assert result['selected'] == {'name': 'MAXN_SUPER', 'id': 7}
  assert calls == [('/usr/sbin/nvpmodel', '-q'), ('/usr/sbin/nvpmodel', '-m', '7'),
                   ('/usr/sbin/nvpmodel', '-q'), ('/usr/bin/jetson_clocks',)]


def test_already_super_verifies_and_locks_without_mode_change(board):
  root, calls, mode = board
  mode[:] = ['MAXN_SUPER', 7]
  assert power.apply(root)['state'] == 'ready'
  assert all('-m' not in call for call in calls)


def test_missing_super_does_not_invent_an_id_or_flash_firmware(board):
  root, calls, _ = board
  (root / 'etc/nvpmodel.conf').write_text('< POWER_MODEL ID=0 NAME=15W >\n# < POWER_MODEL ID=2 NAME=MAXN_SUPER >')
  result = power.apply(root)
  assert result['state'] == 'super_profile_missing'
  assert result['profiles'] == [{'id': 0, 'name': '15W'}]
  assert calls == [('/usr/sbin/nvpmodel', '-q')]


@pytest.mark.parametrize('text', ['< POWER_MODEL ID=x NAME=MAXN_SUPER >',
                                  '< POWER_MODEL ID=2 NAME=MAXN_SUPER >\n< POWER_MODEL ID=2 NAME=15W >'])
def test_invalid_config_is_rejected(text):
  with pytest.raises(ValueError):
    power.profiles(text)


@pytest.mark.parametrize('stdout,returncode,state', [
  ('NVPM WARN: Reboot required for changing to this power mode: 7', 0, 'reboot_required'),
  ('NVPM ERROR: failed', 0, 'mode_apply_failed'),
  ('', 1, 'mode_apply_failed'),
])
def test_failed_or_rebooting_mode_never_claims_ready(board, monkeypatch, stdout, returncode, state):
  root, calls, _ = board
  original = power.command
  def command(*args):
    if '-m' in args:
      return NS(stdout=stdout, stderr='', returncode=returncode)
    return original(*args)
  monkeypatch.setattr(power, 'command', command)
  assert power.apply(root)['state'] == state
  assert all('jetson_clocks' not in call[0] for call in calls)


def test_mode_name_cannot_hide_old_firmware_clock_ceiling(board):
  root, _, _ = board
  (root / 'sys/class/devfreq/17000000.gpu/max_freq').write_text('624750000')
  result = power.apply(root)
  assert result['selected']['name'] == 'MAXN_SUPER'
  assert result['state'] == 'firmware_clock_limit'


def test_missing_clock_sensor_and_failed_query_are_reported(board, monkeypatch):
  root, _, _ = board
  (root / 'sys/class/devfreq/17000000.gpu/max_freq').unlink()
  assert power.apply(root)['state'] == 'setup_failed'
  monkeypatch.setattr(power, 'command', lambda *a: NS(returncode=1, stdout='', stderr=''))
  assert power.apply(root)['state'] == 'setup_failed'


def test_reboot_prompt_is_always_declined(monkeypatch):
  calls = []
  monkeypatch.setattr(power.subprocess, 'run', lambda args, **kw: calls.append((args, kw)))
  power.command('/usr/sbin/nvpmodel', '-m', '7')
  assert calls[0][1]['input'] == 'NO\n'
  assert calls[0][1]['timeout'] == 8


def test_runtime_entry_applies_power_without_starting_an_observer(monkeypatch):
  import install_updates
  calls = []
  monkeypatch.setattr(sys, 'argv', ['install_updates.py', '--power-runtime'])
  monkeypatch.setattr(power, 'run_once', lambda: calls.append('power'))
  monkeypatch.setattr(install_updates.subprocess, 'run', lambda args, **kwargs: calls.append(args))
  runpy.run_path(install_updates.__file__, run_name='__main__')
  assert calls == ['power']


@pytest.mark.parametrize('failure,state', [('query', 'mode_verify_failed'), ('clocks', 'clock_apply_failed')])
def test_verification_and_clock_failures(board, monkeypatch, failure, state):
  root, _, _ = board
  original = power.command
  def command(*args):
    if failure == 'query' and '-m' in args:
      return NS(returncode=0, stdout='', stderr='')
    if failure == 'clocks' and args[0] == '/usr/bin/jetson_clocks':
      return NS(returncode=1, stdout='', stderr='')
    return original(*args)
  monkeypatch.setattr(power, 'command', command)
  assert power.apply(root)['state'] == state


def test_each_boot_applies_once_and_failed_boot_retries_after_reboot(board, monkeypatch):
  root, calls, _ = board
  if sys.platform == 'win32':
    monkeypatch.setitem(sys.modules, 'fcntl', NS(flock=lambda *a: None, LOCK_EX=2))
  (root / 'etc/nvpmodel.conf').write_text('< POWER_MODEL ID=0 NAME=15W >')
  first = power.run_once(root)
  assert first['state'] == 'super_profile_missing'
  assert power.run_once(root) == first and len(calls) == 1
  (root / 'proc/sys/kernel/random/boot_id').write_text('boot2')
  (root / 'etc/nvpmodel.conf').write_text('< POWER_MODEL ID=7 NAME=MAXN_SUPER >')
  assert power.run_once(root)['state'] == 'ready'
  assert power.read_status(root / 'run/carrot-jetlink-power.json')['boot_id'] == 'boot2'


def test_repeated_boots_restore_super_when_vendor_mode_resets(board, monkeypatch):
  root, calls, mode = board
  if sys.platform == 'win32':
    monkeypatch.setitem(sys.modules, 'fcntl', NS(flock=lambda *a: None, LOCK_EX=2))
  for index in range(3):
    mode[:] = ['15W', 0]
    (root / 'proc/sys/kernel/random/boot_id').write_text(f'reboot-{index}')
    assert power.run_once(root)['state'] == 'ready'
    assert mode == ['MAXN_SUPER', 7]
    count = len(calls)
    assert power.run_once(root)['state'] == 'ready'
    assert len(calls) == count
  assert sum(call[0] == '/usr/bin/jetson_clocks' for call in calls) == 3


def test_wrong_board_is_read_only(board):
  root, calls, _ = board
  (root / 'proc/device-tree/model').write_text('Jetson AGX Orin')
  assert power.apply(root)['state'] == 'unsupported_board'
  assert not calls


@pytest.mark.parametrize('state', ['super_profile_missing', 'reboot_required', 'firmware_clock_limit', 'mode_apply_failed'])
def test_power_reason_reaches_existing_health_display(state):
  value = {'severity': 'ok', 'reason': '', 'thermal_severity': 'ok'}
  power_health(value, {'state': state})
  assert value['power_error'] and value['severity'] == 'error'
  assert 'MAXN_SUPER' in value['reason']
  assert value['thermal_severity'] == 'ok'


@pytest.mark.skipif(sys.platform == 'win32', reason='Image systemd symlinks need Linux')
def test_image_ordering_and_installed_helper(tmp_path):
  power.configure(tmp_path, Path(__file__).parent)
  units = tmp_path / 'etc/systemd/system'
  body = (units / 'carrot-jetlink-performance.service').read_text()
  assert 'After=nvpmodel.service' in body
  assert 'Before=carrot-jetlink.service carrot-jetlink-xorg.service carrot-jetlink-hud.service' in body
  assert (tmp_path / 'usr/lib/carrot-jetlink-power/power_setup.py').read_bytes() == Path(power.__file__).read_bytes()
  for name in ('carrot-jetlink', 'carrot-jetlink-xorg', 'carrot-jetlink-hud', 'carrot-jetlink-update-apply'):
    assert 'After=carrot-jetlink-performance.service' in (units / f'{name}.service.d/power.conf').read_text()
  power.configure(tmp_path, Path(__file__).parent)
  assert (units / 'multi-user.target.wants/carrot-jetlink-performance.service').is_symlink()


@pytest.mark.parametrize('raw', [b'[]', b'null', b'{broken', b'x' * 8193, b'{"version":2}',
                                 b'{"version":1}', b'{"version":1,"state":[]}',
                                 b'{"version":1,"state":"invented"}'])
def test_corrupt_or_oversize_status_is_not_success(tmp_path, raw):
  status = tmp_path / 'status.json'
  status.write_bytes(raw)
  assert power.read_status(status) == {}


def test_missing_result_is_visible_and_ready_does_not_hide_thermal_fault():
  value = {'severity': 'ok', 'reason': '', 'thermal_severity': 'ok'}
  power_health(value, {})
  assert value['power_error'] and 'unavailable' in value['reason']
  hot = {'severity': 'error', 'reason': 'thermal fault', 'thermal_severity': 'error'}
  power_health(hot, {'state': 'ready', 'profiles': ['not sent on the wire']})
  assert hot['reason'] == 'thermal fault' and hot['severity'] == 'error'
  assert hot['power_setup'] == {'state': 'ready'}


def test_runtime_power_attempt_precedes_existing_bootstrap_migration(tmp_path, monkeypatch):
  import install_updates
  source = tmp_path / 'current/tools/jetlink'
  source.mkdir(parents=True)
  (source / 'power_setup.py').touch()
  (source / 'boot_update.py').touch()
  calls = []
  monkeypatch.setattr(install_updates.subprocess, 'run', lambda args, **kw: calls.append((args, kw)))
  install_updates.migrate_running_release(tmp_path)
  assert [args[-1] for args, _ in calls] == ['--power-runtime', '--bootstrap-only']
  assert calls[0][0][:3] == ['sudo', '-n', sys.executable]
  assert calls[0][1]['timeout'] == 45


def test_failed_sudo_does_not_disable_existing_bootstrap(tmp_path, monkeypatch, capsys):
  import install_updates
  source = tmp_path / 'current/tools/jetlink'
  source.mkdir(parents=True)
  (source / 'power_setup.py').touch()
  (source / 'boot_update.py').touch()
  calls = []
  def command(args, **kw):
    calls.append(args[-1])
    if args[-1] == '--power-runtime':
      raise power.subprocess.TimeoutExpired(args, 45)
  monkeypatch.setattr(install_updates.subprocess, 'run', command)
  install_updates.migrate_running_release(tmp_path)
  assert calls == ['--power-runtime', '--bootstrap-only']
  assert 'Automatic Super setup unavailable' in capsys.readouterr().out


def test_installed_updater_finishes_power_before_candidate_gpu_process(tmp_path, monkeypatch):
  import update_host
  calls = []
  monkeypatch.setattr(Path, 'is_file', lambda p: p.name in ('nv_tegra_release', 'power_setup.py'))
  monkeypatch.setattr(power, 'run_once', lambda: calls.append('power'))
  def popen(args, **kw):
    calls.append('probe')
    assert args[:4] == ['runuser', '-u', 'jetlink', '--']
    assert kw['start_new_session']
    return NS(wait=lambda **kw: 0)
  monkeypatch.setattr(update_host.subprocess, 'Popen', popen)
  update_host.probe_release(tmp_path)
  assert calls == ['power', 'probe']


def test_park_permission_lost_during_power_never_launches_gpu_probe(tmp_path, monkeypatch):
  import update_host
  monkeypatch.setattr(Path, 'is_file', lambda p: p.name in ('nv_tegra_release', 'power_setup.py'))
  monkeypatch.setattr(power, 'run_once', lambda: None)
  monkeypatch.setattr(update_host.subprocess, 'Popen', lambda *a, **kw: pytest.fail('GPU probe after lost permission'))
  with pytest.raises(update_host.ActivationDeferred, match='Park permission expired'):
    update_host.probe_release(tmp_path, allowed=lambda: False)


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux image symlinks need Linux')
def test_image_installer_copies_helper_and_orders_update_apply(tmp_path):
  import install_updates
  install_updates.configure(tmp_path, Path(__file__).parent)
  assert (tmp_path / 'opt/carrot-jetlink/updater/power_setup.py').read_bytes() == Path(power.__file__).read_bytes()
  unit = tmp_path / 'etc/systemd/system/carrot-jetlink-update-apply.service.d/power.conf'
  assert 'After=carrot-jetlink-performance.service' in unit.read_text()
  assert (tmp_path / 'opt/carrot-jetlink/boot-update-required').read_text() == 'next-boot\n'
