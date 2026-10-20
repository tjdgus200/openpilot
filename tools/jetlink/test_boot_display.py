import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boot_display import lines, render, panel_frame
import boot_status


def test_offline_diagnosis_without_comma_model_or_xorg(tmp_path):
  title, rows, detail, error = lines({'stage': {'stage': 'root-readonly-check', 'error': 'RuntimeError'},
                                    'units': {'carrot-protected-storage': {'ActiveState': 'failed'}}})
  assert error and title[1] == 'BOOT NEEDS ATTENTION'
  assert rows[3][2] == 'Waiting for network'
  assert 'carrot-protected-storage' in detail
  # The frame is CPU-only and fully renderable without raylib/TensorRT/Xorg.
  frame = render({'stage': {'stage': 'root-readonly-check', 'error': 'RuntimeError'},
                  'uptime': 25})
  assert frame.size == (1920, 462)
  frame.save(tmp_path / 'boot-error.png')


def test_active_service_does_not_claim_model_ready():
  title, rows, _, _ = lines({'units': {'carrot-jetlink': {'ActiveState': 'active'}}})
  assert 'WAITING' in title[1]
  assert rows[5] == ('콤마 USB 서비스', 'COMMA USB SERVICE', 'active')


def test_unreadable_thermal_sensor_does_not_hide_boot_diagnosis(monkeypatch):
  original = Path.read_text

  def read(self, *args, **kwargs):
    if self.as_posix().endswith('/thermal_zone0/temp'):
      # Observed on Jetson Python3.10 when a sysfs read returns EAGAIN.
      raise TypeError("can't concat NoneType to bytes")
    if self.as_posix().endswith('/thermal_zone1/temp'):
      return '55000'
    return original(self, *args, **kwargs)

  monkeypatch.setattr(Path, 'read_text', read)
  monkeypatch.setattr(Path, 'glob', lambda self, pattern: iter([
    Path('/sys/class/thermal/thermal_zone0/temp'),
    Path('/sys/class/thermal/thermal_zone1/temp')]))
  monkeypatch.setattr(boot_status.subprocess, 'run', lambda *a, **k:
                      subprocess.CompletedProcess(a, 0, stdout='[]', stderr=''))
  status = boot_status.collect()
  assert status['temp_c'] == 55
  assert render(status).size == (1920, 462)


def test_panel_uses_existing_portrait_upload_geometry():
  from PIL import Image
  frame = Image.new('RGB', (1920, 462))
  frame.putpixel((0, 0), (255, 0, 0))
  frame.putpixel((1919, 461), (0, 255, 0))
  portrait = panel_frame(frame)
  assert portrait.size == (462, 1920)
  assert portrait.getpixel((461, 0)) == (255, 0, 0)
  assert portrait.getpixel((0, 1919)) == (0, 255, 0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Offline Linux service installation')
def test_pinned_diagnostic_install_is_independent_of_model_and_xorg(tmp_path):
  from install_boot_display import configure
  configure(tmp_path, Path(__file__).parent)
  pinned = tmp_path / 'usr/local/lib/carrot-jetlink/boot-display'
  service = (tmp_path / 'etc/systemd/system/carrot-jetlink-boot-display.service').read_text()
  assert 'Requires=' not in service and 'After=local-fs.target systemd-udev-trigger.service' in service
  assert '/opt/carrot-jetlink/current/tools/jetlink/boot_display.py' not in service
  code = '''import sys
sys.path.insert(0, sys.argv[1] + '/tools/jetlink')
sys.path.insert(0, sys.argv[2])
import boot_display
import cluster_usb_display
assert not any(name in sys.modules for name in ('pyray', 'tensorrt', 'capnp'))
assert boot_display.render({'uptime':1}).size == (1920,462)
'''
  import PIL
  subprocess.run([sys.executable, '-I', '-c', code, str(pinned), str(Path(PIL.__file__).parents[1])], check=True)


def test_boot_record_does_not_persist_arbitrary_input(tmp_path, monkeypatch):
  original = Path.read_text
  monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k:
                      'test-boot-id' if self.as_posix() == '/proc/sys/kernel/random/boot_id' else original(self, *a, **k))
  boot_status.persist_result(tmp_path, {'stage': 'protected', 'password': 'must-not-save',
                                        'journal': 'private log', 'error': ''})
  value = json.loads((tmp_path / 'BOOT-STATUS.json').read_text())
  assert value == {'format': 1, 'boot_id': 'test-boot-id', 'stage': 'protected', 'error': ''}


@pytest.mark.skipif(sys.platform != 'linux', reason='Real flock/process identity test')
def test_panel_handoff_and_crashed_hud_release(tmp_path):
  from display_owner import hud_requested, panel_owner
  code = '''
import sys,time
from pathlib import Path
from display_owner import panel_owner
with panel_owner(hud=True, directory=Path(sys.argv[1])) as acquired:
 print('HUD_OWNS' if acquired else 'ERROR', flush=True)
 time.sleep(30)
'''
  child = None
  try:
    with panel_owner(directory=tmp_path) as acquired:
      assert acquired
      child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                               env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)),
                               stdout=subprocess.PIPE, text=True)
      deadline = time.monotonic() + 5
      while not hud_requested(tmp_path) and time.monotonic() < deadline:
        time.sleep(.02)
      assert hud_requested(tmp_path)
      assert child.poll() is None
    assert child.stdout.readline().strip() == 'HUD_OWNS'
    with panel_owner(directory=tmp_path) as acquired:
      assert not acquired
    child.kill()
    child.wait(timeout=5)
    with panel_owner(directory=tmp_path) as acquired:
      assert acquired
    (tmp_path / 'hud-request').write_text(json.dumps({'pid': os.getpid(), 'identity': ['old-boot', '0']}))
    assert not hud_requested(tmp_path)
  finally:
    if child and child.poll() is None:
      child.kill()
      child.wait(timeout=5)


def test_runtime_display_override_preserves_immutable_recovery(tmp_path, monkeypatch):
  import install_updates
  target = tmp_path / 'run/systemd/system/carrot-jetlink-boot-display.service.d/runtime.conf'
  monkeypatch.setattr(install_updates, 'BOOT_DISPLAY_DROPIN', target)
  calls = []
  monkeypatch.setattr(install_updates.subprocess, 'run', lambda command, **kwargs: calls.append(command))
  install_updates.configure_runtime_boot_display()
  assert target.read_text() == install_updates.BOOT_DISPLAY_OVERRIDE
  assert not (tmp_path / 'etc').exists()
  assert calls == [['systemctl', 'daemon-reload'], ['systemctl', 'try-restart', 'carrot-jetlink-boot-display']]
  calls.clear()
  install_updates.migrate_boot_display(tmp_path)
  assert calls == []  # Never restart a healthy panel at every server reconnect.


def test_panel_lock_is_outside_login_managed_shared_memory():
  from display_owner import DIRECTORY
  assert DIRECTORY.parent in (Path('/run'), Path('/tmp'))


def test_update_failure_and_available_space_visible_without_comma():
  value = {'update': {'state': 'failed', 'phase': 'begin', 'error_code': 'insufficient_space',
                      'required_bytes': 6087449262}, 'power_w': 7.5, 'temp_c': 54.5,
           'data': {'free_bytes': 5911064576, 'total_bytes': 16795955200}}
  title, rows, detail, error = lines(value)
  assert error and title[1] == 'BOOT NEEDS ATTENTION'
  assert rows[6][2] == '업데이트 실패'
  assert '5.91 / 16.80 GB free' == rows[7][2]
  assert rows[8][2] == '전송 시작 전'
  assert 'DATA 여유 공간 부족' in detail and '6.09 GB' in detail
  assert render(value).size == (1920, 462)


@pytest.mark.parametrize('state', ['receiving', 'downloading'])
def test_live_update_percentage_and_stale_status(state):
  _, rows, _, error = lines({'update': {'state': state, 'fraction': .425}})
  assert not error and '42.5%' in rows[6][2]
  _, rows, _, _ = lines({'update': {'state': state, 'fraction': .425, 'stale': True}})
  assert '상태 갱신 중단' in rows[6][2]


def test_status_filters_private_errors_and_invalid_update_numbers(monkeypatch):
  monkeypatch.setattr(boot_status.time, 'monotonic', lambda: 20.)
  monkeypatch.setattr(boot_status, 'read_json', lambda path: {
    'state': 'failed', 'updated': 19., 'fraction': float('nan'),
    'error': 'private credential text must not render', 'url': 'private url',
    'required_bytes': True, 'free_bytes': -1, 'phase': 'begin'})
  value = boot_status.update_status()
  assert value == {'state': 'failed', 'stale': False, 'phase': 'begin', 'error_code': 'activation_failed'}
  assert 'private' not in str(lines({'update': value}))


def test_completed_update_remains_valid_only_for_matching_boot_and_release(tmp_path, monkeypatch):
  update, ready, boot = (tmp_path / name for name in ('update', 'ready', 'boot'))
  current = tmp_path / 'runtime/current';current.mkdir(parents=True)
  (current / 'SOURCE_COMMIT').write_text('a' * 40)
  boot.write_text('boot1')
  update.write_text(json.dumps({'state': 'ready', 'updated': 1.}))
  ready.write_text(json.dumps({'boot_id': 'boot1', 'source_commit': 'a' * 40}))
  for name, value in (('UPDATE', update), ('READY', ready), ('BOOT_ID', boot), ('RUNTIME', tmp_path / 'runtime')):
    monkeypatch.setattr(boot_status, name, value)
  monkeypatch.setattr(boot_status.time, 'monotonic', lambda: 100.)
  assert not boot_status.update_status()['stale']
  boot.write_text('boot2')
  assert boot_status.update_status()['stale']
  boot.write_text('boot1');(current / 'SOURCE_COMMIT').write_text('b' * 40)
  assert boot_status.update_status()['stale']


def test_persistent_diagnostic_override_is_explicit_root_install_only(tmp_path, monkeypatch):
  if getattr(os, 'geteuid', lambda: -1)() != 0:
    pytest.skip('Linux root-owned diagnostic installation')
  import boot_update as boot
  import install_boot_display as installer
  runtime, base = tmp_path / 'runtime', tmp_path / 'immutable-display'
  runtime.mkdir();base.mkdir()
  (base / 'tools/jetlink').mkdir(parents=True)
  (base / 'asset.txt').write_text('immutable asset')
  monkeypatch.setattr(boot.update, 'ROOT', runtime)
  dropin = tmp_path / 'run/zz-owner-diagnostics.conf'
  monkeypatch.setattr(boot, 'DIAGNOSTICS_DROPIN', dropin)
  calls = []
  monkeypatch.setattr(boot.subprocess, 'run', lambda command, **kw: calls.append(command))
  boot.configure_diagnostic_display()
  assert not dropin.exists() and not calls
  installer.configure_owner(runtime, Path(__file__).parent, base)
  boot.configure_diagnostic_display()
  assert str(runtime / 'boot-display/tools/jetlink/boot_display.py') in dropin.read_text()
  assert calls == [['systemctl', 'daemon-reload'], ['systemctl', 'try-restart', 'carrot-jetlink-boot-display']]
  calls.clear();boot.configure_diagnostic_display()
  assert not calls  # No panel flicker at every gate reconnect.
  installer.configure_owner(runtime, Path(__file__).parent, base)
  assert (runtime / 'boot-display.previous/.owner-installed').read_text() == '1\n'
  (runtime / 'boot-display').chmod(0o777)
  dropin.unlink();boot.configure_diagnostic_display()
  assert not dropin.exists() and not calls
