import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from hud import require_usb_display, wait_for_usb_display, wait_for_hud_input


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


@pytest.mark.parametrize('params', [{}, {'ClusterHud': 'MA==', 'IsOnroad': 'MA==', 'ClusterHudDebug': 'MA=='}])
def test_first_hud_data_starts_display_without_ignition_or_debug(params):
  snapshot = (1., {'version': 1, 'params': params})
  records = iter([None, None, snapshot])
  sleeps = []
  assert wait_for_hud_input(lambda: next(records), sleeps.append) == snapshot
  assert sleeps == [.1, .1]


def test_handoff_uses_actual_snapshot_expiry_and_waits_through_stale_future_and_missing_input(tmp_path, monkeypatch):
  import json
  import time
  import hud_protocol as protocol
  packet = tmp_path/'snapshot.packet'
  monkeypatch.setattr(protocol, 'HUD_PACKET', packet)
  monkeypatch.setattr(protocol, '_cached_snapshot', None)
  monkeypatch.setattr(protocol, '_next_snapshot_read', 0.)
  payload = json.dumps({'version': 1, 'params': {}}).encode()
  def write(age):
    packet.write_bytes(protocol.HEADER.pack(time.monotonic() - age) + payload)
    protocol._next_snapshot_read = 0.
  write(10.)
  sleeps = []
  def sleep(delay):
    sleeps.append(delay)
    if len(sleeps) == 1:
      write(-100.)  # A future timestamp is not readiness.
    elif len(sleeps) == 2:
      packet.unlink()
      protocol._next_snapshot_read = 0.
    else:
      write(.01)
  result = wait_for_hud_input(protocol.read_snapshot, sleep)
  assert result[1]['params'] == {} and sleeps == [.1, .1, .1]


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
