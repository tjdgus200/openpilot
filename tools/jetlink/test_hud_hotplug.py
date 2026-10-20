import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from hud import require_usb_display, wait_for_usb_display


def test_host_switch_is_independent_but_ignition_and_preferences_are_forwarded(monkeypatch):
  import base64
  import hud
  settings = {'ClusterHud': '0', 'IsOnroad': '0', 'ClusterHudDebug': '0', 'ClusterHudBrightness': '42'}
  monkeypatch.setattr(hud, 'read_snapshot', lambda: (1., {'params': {
    key: base64.b64encode(value.encode()).decode() for key, value in settings.items()}}))
  params = hud.DisplayParams()
  assert params.get_int('ClusterHud') == 1
  assert not params.get_bool('IsOnroad') and params.get_int('ClusterHudDebug') == 0
  assert params.get_int('ClusterHudBrightness') == 42
  settings['IsOnroad'] = '1'
  params.next_read = 0.
  assert params.get_bool('IsOnroad') and params.get_int('ClusterHud') == 1


def test_absent_display_waits_until_it_arrives_without_window_fallback():
  replies = iter([None, None, 0x0092])
  sleeps = []
  expected = []
  def scan(product):
    expected.append(product)
    return next(replies)
  wait_for_usb_display(scan, 0x0092, sleeps.append)
  assert sleeps == [1., 1.] and expected == [0x0092] * 3


def test_renderer_preparation_does_not_require_vehicle_params_or_take_panel(monkeypatch):
  import hud
  stages = []
  monkeypatch.setattr(hud, 'report_stage', stages.append)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: pytest.fail('startup waited for vehicle data'))
  hud.prepare_renderer(lambda: stages.append('runtime_approved'))
  assert stages == ['waiting_runtime', 'runtime_approved', 'loading_renderer']


def test_dashboard_startup_does_not_bypass_failed_runtime_validation(monkeypatch):
  import hud
  stages = []
  monkeypatch.setattr(hud, 'report_stage', stages.append)
  def reject():
    raise RuntimeError('runtime not approved')
  with pytest.raises(RuntimeError):
    hud.prepare_renderer(reject)
  assert stages == ['waiting_runtime']


def test_unplug_between_scan_and_open_retries_instead_of_window_mode():
  with pytest.raises(RuntimeError, match='disappeared'):
    require_usb_display(lambda expected: None, 0x0092)
  assert require_usb_display(lambda expected: expected, 0x0092) == 0x0092


def test_remote_camera_supports_renderer_signal_interface_without_fabricated_evidence(tmp_path):
  from hud_camera import RemoteRoadCamera
  from cluster_renderer import ClusterUiRenderer
  from openpilot.selfdrive.carrot.signal_display import SignalDisplayReader, valid_record
  camera = RemoteRoadCamera()
  camera.last_frame = ('road', 42)  # A JPEG preview is still not a full signal frame.
  assert camera.display_frame is None
  assert not valid_record({}, 10., camera.display_frame)
  renderer = object.__new__(ClusterUiRenderer)
  renderer._signal_display_reader = SignalDisplayReader(flag=tmp_path / 'disabled')
  # Exercise the exact render path that previously raised AttributeError.
  renderer._draw_signal_detections(None, camera)


@pytest.fixture
def display_input(monkeypatch):
  import base64
  import boot_status, boot_update, hud
  from openpilot.cereal import log
  event = log.Event.new_message(valid=True, deviceState={})
  value = {'sent': 20., 'received': {'deviceState': 19.9},
           'valid': {'deviceState': True, 'carState': False, 'modelV2': False},
           'alive': {'deviceState': True}, 'events': {'deviceState': base64.b64encode(event.to_bytes()).decode()}}
  states = {'/dev/shm/carrot-jetlink-hud-wifi.json': {'updated': 9.9, 'role': 'receive', 'snapshot': 'connected'},
            '/dev/shm/carrot-jetlink-health.json': {'updated': 9.9, 'severity': 'ok'},
            '/run/carrot-storage.json': {'state': 'protected'}}
  monkeypatch.setattr(hud.time, 'monotonic', lambda: 10.)
  monkeypatch.setattr(boot_update, 'enabled', lambda: False)
  monkeypatch.setattr(boot_status, 'read_json', lambda path: states.get(str(path), {}))
  monkeypatch.setattr(hud, 'read_snapshot', lambda: (9.9, value))
  return states, value


def test_normal_offroad_can_handoff_without_vehicle_can_or_active_cruise(display_input):
  import hud
  assert hud.display_readiness() == ''


@pytest.mark.parametrize('change,reason', [
  ('wifi_stale', 'wifi_not_ready'), ('wifi_disconnected', 'wifi_not_ready'),
  ('health_missing', 'host_health_missing'), ('health_error', 'host_health_error'),
  ('invalid_device', 'device_state_invalid'), ('old_device', 'device_state_invalid'),
  ('corrupt_event', 'device_state_invalid'), ('wrong_event', 'device_state_invalid')])
def test_unhealthy_data_cannot_take_boot_panel(display_input, change, reason):
  import base64,hud
  from openpilot.cereal import log
  states, value = display_input
  if change == 'wifi_stale': states['/dev/shm/carrot-jetlink-hud-wifi.json']['updated'] = 1.
  elif change == 'wifi_disconnected': states['/dev/shm/carrot-jetlink-hud-wifi.json']['snapshot'] = 'waiting_wifi'
  elif change == 'health_missing': states.pop('/dev/shm/carrot-jetlink-health.json')
  elif change == 'health_error': states['/dev/shm/carrot-jetlink-health.json']['severity'] = 'error'
  elif change == 'invalid_device': value['valid']['deviceState'] = False
  elif change == 'old_device': value['received']['deviceState'] = 10.
  elif change == 'corrupt_event': value['events']['deviceState'] = 'not-valid-base64!'
  elif change == 'wrong_event': value['events']['deviceState'] = base64.b64encode(log.Event.new_message(valid=True, carState={}).to_bytes()).decode()
  assert hud.display_readiness() == reason


def test_runtime_failure_or_missing_snapshot_cannot_handoff(display_input, monkeypatch):
  import boot_update,hud
  monkeypatch.setattr(boot_update, 'enabled', lambda: True)
  monkeypatch.setattr(boot_update, 'runtime_ready', lambda: False)
  assert hud.display_readiness() == 'runtime_not_ready'
  monkeypatch.setattr(boot_update, 'runtime_ready', lambda: True)
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  assert hud.display_readiness() == 'hud_snapshot_missing'


def test_handoff_requires_continuous_good_input_and_preserves_boot_request(monkeypatch, tmp_path):
  import hud
  from display_owner import panel_owner,hud_requested
  clock = [0.];count = [0];reports = []
  def sleep(seconds):clock[0] += seconds
  def check():
    count[0] += 1
    return 'wifi_not_ready' if count[0] in (1, 4) else ''
  monkeypatch.setattr(hud, 'report_stage', lambda stage, **kw: reports.append((stage, kw)))
  hud.wait_for_display_ready(check, sleep, lambda: clock[0])
  assert clock[0] >= .9 and len(reports) == 2
  with panel_owner(directory=tmp_path) as boot:
    assert boot
    with panel_owner(hud=True, directory=tmp_path, ready=lambda: False) as acquired:
      assert not acquired and not hud_requested(tmp_path)
  assert not (tmp_path / 'hud-request').exists()
