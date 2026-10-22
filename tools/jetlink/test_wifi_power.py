"""Power-state enforcement must not reconnect or block the model service."""
import json
from types import SimpleNamespace as NS

import pytest

import wifi_power as power


def row(name='wlP1p1s0', index=4, kind=2):
  return {'name': name, 'index': index, 'type': kind, 'phy': 0}


def test_only_stations_change_and_reconnect_power_is_rechecked():
  values = {4: 1, 5: 0}
  writes = []
  def set_power(index, enabled):
    writes.append((index, enabled))
    values[index] = enabled
  radio = NS(interfaces=lambda: [row(), row('wlan1', 5), row('wl-ap', 6, 3), row('eth0', 7)],
             power_save=lambda index: values[index], set_power_save=set_power)
  assert power.enforce(radio) == [{'interface': 'wlP1p1s0', 'power_save': 0},
                                 {'interface': 'wlan1', 'power_save': 0}]
  assert writes == [(4, 0)]
  power.enforce(radio)
  assert writes == [(4, 0)]  # Healthy connections receive no repeated SET.
  values[4] = 1  # NetworkManager/driver re-enabled it after association.
  power.enforce(radio)
  assert writes == [(4, 0), (4, 0)]


def test_removed_or_reused_interface_is_not_changed():
  rows = iter([[row()], [row('wl-other')]])
  radio = NS(interfaces=lambda: next(rows), power_save=lambda _: 1,
             set_power_save=lambda *a: pytest.fail('reused interface was changed'))
  assert power.enforce(radio) == []


def test_driver_refusal_cannot_report_success():
  radio = NS(interfaces=lambda: [row()], power_save=lambda _: 1,
             set_power_save=lambda *a: None)
  with pytest.raises(OSError):
    power.enforce(radio)


def test_startup_returns_without_waiting_for_root_service(tmp_path, monkeypatch):
  marker = tmp_path / 'tegra'
  marker.touch()
  script = tmp_path / 'current/tools/jetlink/wifi_power.py'
  script.parent.mkdir(parents=True)
  script.touch()
  monkeypatch.setattr(power, 'TEGRA', marker)
  pending = []
  threads = []
  class Thread:
    def __init__(self, *, target, name, daemon):
      assert daemon is True
      self.target = target
      threads.append(self)
    def start(self):
      pending.append(self.target)  # No synchronous command or wait.
  monkeypatch.setattr(power, 'threading', NS(Thread=Thread))
  calls = []
  def run(command, **kwargs):
    calls.append((command, kwargs))
    raise power.subprocess.TimeoutExpired('systemd-run', 10)
  monkeypatch.setattr(power.subprocess, 'run', run)
  power.schedule(tmp_path)
  assert calls == []
  assert len(threads) == len(pending) == 1
  pending[0]()  # A launch timeout must not escape into GPU service startup.
  command, kwargs = calls[0]
  assert command[:3] == ['sudo', '-n', 'systemd-run']
  for setting in ('Nice=19', 'CPUSchedulingPolicy=other', 'CPUAffinity=0 1',
                  'CapabilityBoundingSet=CAP_NET_ADMIN', 'NoNewPrivileges=yes',
                  'ProtectSystem=strict', 'RestrictAddressFamilies=AF_NETLINK AF_UNIX'):
    assert '--property=' + setting in command
  assert command[-1] == str(script)
  assert kwargs['timeout'] == 10


def test_other_hosts_do_not_schedule_root_work(tmp_path, monkeypatch):
  monkeypatch.setattr(power, 'TEGRA', tmp_path / 'missing')
  monkeypatch.setattr(power, 'threading', NS(Thread=lambda **kw: pytest.fail('non-Jetson root work')))
  power.schedule(tmp_path)


def test_thread_creation_failure_does_not_stop_model_startup(tmp_path, monkeypatch):
  marker = tmp_path / 'tegra'
  marker.touch()
  script = tmp_path / 'current/tools/jetlink/wifi_power.py'
  script.parent.mkdir(parents=True)
  script.touch()
  monkeypatch.setattr(power, 'TEGRA', marker)
  def exhausted(**kwargs):
    raise RuntimeError('synthetic thread exhaustion')
  monkeypatch.setattr(power, 'threading', NS(Thread=exhausted))
  power.schedule(tmp_path)


def test_driver_failure_backs_off_without_network_reset(tmp_path, monkeypatch):
  marker = tmp_path / 'tegra'
  marker.touch()
  monkeypatch.setattr(power, 'TEGRA', marker)
  monkeypatch.setattr(power.os, 'geteuid', lambda: 0, raising=False)
  monkeypatch.setattr(power.signal, 'signal', lambda *a: None)
  delays = []
  event = NS(is_set=lambda: len(delays) == 2, set=lambda: None, wait=delays.append)
  monkeypatch.setattr(power, 'threading', NS(Event=lambda: event))
  closed = []
  def failure():
    raise OSError('synthetic driver failure')
  radios = [NS(interfaces=failure, close=lambda: closed.append('failed')),
            NS(interfaces=list, close=lambda: closed.append('healthy'))]
  radios_iter = iter(radios)
  monkeypatch.setattr(power, 'Wireless', lambda: next(radios_iter))
  records = []
  monkeypatch.setattr(power, 'publish', lambda state, interfaces: records.append((state, interfaces)))
  power.main()
  assert records == [('netlink_unavailable', []), ('waiting_station', [])]
  assert delays == [30., 5.]
  assert closed == ['failed', 'healthy']


def test_status_contains_no_network_credentials(tmp_path):
  path = tmp_path / 'status.json'
  power.publish('disabled', [{'interface': 'wlP1p1s0', 'power_save': 0}], path)
  result = json.loads(path.read_text())
  assert set(result) == {'state', 'updated', 'interfaces'}
  assert result['interfaces'] == [{'interface': 'wlP1p1s0', 'power_save': 0}]
  assert list(tmp_path.iterdir()) == [path]


def test_installed_boot_policy_does_not_skip_power_worker(tmp_path, monkeypatch):
  import install_updates
  (tmp_path / 'current/tools/jetlink').mkdir(parents=True)
  (tmp_path / 'current/tools/jetlink/boot_update.py').touch()
  (tmp_path / 'boot-update-required').touch()
  calls = []
  schedule = calls.append
  monkeypatch.setattr(power, 'schedule', schedule)
  install_updates.migrate_running_release(tmp_path)
  assert calls == [tmp_path]


def test_early_profile_migration_preserves_secrets_and_unowned_files(tmp_path):
  import wifi_boot
  original = b'[connection]\nid=private\ntype=wifi\n[wifi]\nmode=infrastructure\nssid=private\\sSSID\npowersave=3\n[wifi-security]\npsk=private\\svalue\n'
  owned = tmp_path / 'carrot-usb-example.nmconnection'
  owned.write_bytes(original)
  other = tmp_path / 'administrator.nmconnection'
  other.write_bytes(original)
  changes = wifi_boot.profiles_off(tmp_path)
  assert list(changes) == [owned]
  before, after = changes[owned]
  assert before == original and after == original.replace(b'powersave=3', b'powersave=2')
  assert owned.read_bytes() == other.read_bytes() == original  # Prepare before mutation.
  owned.write_bytes(after)
  assert wifi_boot.profiles_off(tmp_path) == {}


def test_early_profile_migration_rejects_symlinks(tmp_path):
  import os
  import wifi_boot
  if os.name == 'nt':
    pytest.skip('Linux symlink ownership')
  target = tmp_path / 'administrator.nmconnection'
  target.write_text('[wifi]\nmode=infrastructure\n')
  (tmp_path / 'carrot-usb-not-owned.nmconnection').symlink_to(target)
  with pytest.raises(ValueError):
    wifi_boot.profiles_off(tmp_path)


def test_boot_gate_schedules_policy_before_usb_or_model_start(monkeypatch):
  import boot_update
  import wifi_boot
  import update_host
  events = []
  monkeypatch.setattr(wifi_boot, 'schedule', lambda runtime: events.append(('policy', runtime)))
  monkeypatch.setattr(boot_update, 'runtime_ready', lambda: True)
  monkeypatch.setattr(boot_update.subprocess, 'run', lambda *a, **k: events.append(('services', a)))
  boot_update.boot()
  assert events[0] == ('policy', update_host.ROOT)
  assert events[1][0] == 'services'


def test_new_bootstrap_can_refresh_without_resetting_usb_boot_approval(tmp_path):
  import install_updates
  source = __import__('pathlib').Path(__file__).parent
  runtime = tmp_path / 'opt/carrot-jetlink'
  runtime.mkdir(parents=True)
  marker = runtime / 'boot-update-required'
  marker.write_bytes(b'previous-boot-policy\n')
  install_updates.bootstrap(tmp_path, source, preserve_policy=True, sync=False)
  assert marker.read_bytes() == b'previous-boot-policy\n'
  assert (runtime / 'updater/wifi_boot.py').read_bytes() == (source / 'wifi_boot.py').read_bytes()
  assert (runtime / 'updater/wifi_apply.py').read_bytes() == (source / 'wifi_apply.py').read_bytes()


def test_early_prepare_reloads_defaults_and_only_restarts_importer_once(tmp_path, monkeypatch):
  import wifi_boot
  monkeypatch.setattr(wifi_boot, 'CONFIG', tmp_path / 'nm.conf')
  monkeypatch.setattr(wifi_boot, 'DROPIN', tmp_path / 'network.conf')
  monkeypatch.setattr(wifi_boot, 'STATUS', tmp_path / 'status.json')
  calls = []
  monkeypatch.setattr(power, 'schedule', lambda runtime: calls.append(('power', runtime)))
  monkeypatch.setattr(wifi_boot.subprocess, 'run', lambda command, **kw: calls.append((command, kw)))
  wifi_boot.prepare(tmp_path)
  assert calls[0] == ('power', tmp_path)
  commands = [c for c, _ in calls[1:]]
  assert commands == [['nmcli', 'general', 'reload', 'conf'], ['systemctl', 'daemon-reload'],
                      ['systemctl', 'try-restart', 'carrot-jetlink-wifi.service']]
  assert wifi_boot.CONFIG.read_bytes() == wifi_boot.DEFAULTS
  before = list(calls)
  wifi_boot.prepare(tmp_path)
  assert calls == before  # No repeated network restart on a USB gate retry.
