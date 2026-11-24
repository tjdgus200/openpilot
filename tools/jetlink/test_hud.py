import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import hud_protocol as p
from hud_camera import preview_nv12, WIDTH, HEIGHT


def test_preview_preserves_full_camera_field_and_uv_order():
  width, height, stride = WIDTH * 2, HEIGHT * 2, WIDTH * 2 + 16
  y = np.arange(height * stride, dtype=np.uint8).reshape(height, stride)
  uv = np.zeros((height // 2, stride), np.uint8)
  uv[:, 0::2] = 71
  uv[:, 1::2] = 183
  frame = SimpleNamespace(width=width, height=height, stride=stride, uv_offset=height * stride,
                          data=y.tobytes() + uv.tobytes() + b'padding beyond NV12 planes')
  out = np.frombuffer(preview_nv12(frame), np.uint8)
  np.testing.assert_array_equal(out[:WIDTH * HEIGHT].reshape(HEIGHT, WIDTH), y[::2, :width:2])
  assert np.all(out[WIDTH * HEIGHT::2] == 71)
  assert np.all(out[WIDTH * HEIGHT + 1::2] == 183)


def test_stale_and_oversized_display_data_are_not_shown(tmp_path, monkeypatch):
  path = tmp_path / 'hud'
  monkeypatch.setattr(p, 'HUD_PACKET', path)
  monkeypatch.setattr(p.time, 'monotonic', lambda: 10.)
  for data in (p.HEADER.pack(9.) + b'{"version":1}', b'x' * (p.MAX_HUD_BYTES + 20), b'bad'):
    path.write_bytes(data)
    monkeypatch.setattr(p, '_next_snapshot_read', 0.)
    assert p.read_snapshot() is None
  path.write_bytes(p.HEADER.pack(9.9) + json.dumps({'version':1, 'params':{}}).encode())
  monkeypatch.setattr(p, '_next_snapshot_read', 0.)
  assert p.read_snapshot()[1]['version'] == 1


def test_publisher_does_not_replay_stale_or_duplicate_snapshots(tmp_path, monkeypatch):
  publisher = object.__new__(p.Publisher)
  publisher.path = tmp_path / 'publisher'
  publisher.last_sent = 0.
  monkeypatch.setattr(p.time, 'monotonic', lambda: 10.)
  assert publisher.packet() is None
  publisher.path.write_bytes(p.HEADER.pack(9.9) + b'latest')
  assert publisher.packet() == b'latest'
  assert publisher.packet() is None
  publisher.path.write_bytes(p.HEADER.pack(8.) + b'stale')
  assert publisher.packet() is None
  publisher.path.write_bytes(p.HEADER.pack(9.99) + b'x' * (96 * 1024 + 1))
  assert publisher.packet() is None


def test_lane_probabilities_use_existing_model_snapshot_without_new_wire_fields(monkeypatch):
  import hud
  from openpilot.cereal import log
  from openpilot.common.hud_driving_feedback import lane_line_probabilities

  event = log.Event.new_message(modelV2={})
  event.modelV2.laneLineProbs = [1., .95, .82, 1.]
  event.modelV2.confidence = 'red'
  record = {'sent': 100., 'valid': {'modelV2': True}, 'alive': {'modelV2': True},
            'received': {'modelV2': 100.}, 'mono': {'modelV2': 100_000_000_000},
            'events': {'modelV2': base64.b64encode(event.to_bytes()).decode()}}
  monkeypatch.setattr(hud, 'read_snapshot', lambda: (100., record))
  remote = hud.RemoteSubMaster(['modelV2'])
  remote.update()
  left, right = lane_line_probabilities(remote['modelV2'])
  assert abs(left - .95) < 1e-6 and abs(right - .82) < 1e-6
  assert remote.valid['modelV2'] and remote.alive['modelV2']
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  remote.update()
  assert not remote.valid['modelV2'] and not remote.alive['modelV2']


def test_pv5_hud_oem_lane_markings_follow_existing_snapshot_and_expire(monkeypatch):
  import hud
  from cluster_config import BLUE, WHITE, YELLOW
  from cluster_live import OpenpilotLiveSource, live_route_parser, standby_state
  from cluster_scene import build_cluster_scene, rgba
  from openpilot.cereal import log

  services = ['carState', 'carParams', 'modelV2']
  source = object.__new__(OpenpilotLiveSource)
  source.sm = hud.RemoteSubMaster(services)
  source.services = services
  source.parser = live_route_parser()
  source._standby_state = standby_state()
  source.profile_enabled, source.timeout_ms, source.frames = False, 0, 0
  source._with_debug_state = source._with_live_hud_state = lambda state: state
  local_time = 25.0
  received = local_time
  packet = None
  monkeypatch.setattr(hud.time, 'monotonic', lambda: local_time)
  monkeypatch.setattr(hud.time, 'monotonic_ns', lambda: round(local_time * 1e9))
  monkeypatch.setattr(hud, 'read_snapshot', lambda: (received, packet) if packet is not None else None)

  def relay_update(sender_time, camera_time, fingerprint='KIA_PV5', left=21, right=35):
    nonlocal received
    received = local_time
    stamp = round(sender_time * 1e9)
    car_state = log.Event.new_message(logMonoTime=stamp, valid=True, carState={
      'canValid': True, 'gearShifter': 'drive', 'vEgo': 10.0, 'leftLaneLine': left, 'rightLaneLine': right,
      'pv5LaneMarking': {
        'displayMonoTime': round(camera_time * 1e9), 'leftDisplayCode': left, 'rightDisplayCode': right,
      },
    })
    car_params = log.Event.new_message(logMonoTime=stamp, valid=True, carParams={
      'brand': 'hyundai', 'carFingerprint': fingerprint,
    })
    model = log.Event.new_message(logMonoTime=stamp, valid=True, modelV2={
      'laneLineProbs': [.9, .9, .9, .9],
      'laneLines': [{'x': [0., 20., 50.], 'y': [y, y, y]} for y in (-5.4, -1.8, 1.8, 5.4)],
    })
    events = dict(zip(services, (car_state, car_params, model), strict=True))
    return json.loads(p.encode_snapshot({
      'version': 1, 'sent': sender_time, 'events': {
        name: base64.b64encode(event.to_bytes()).decode() for name, event in events.items()
      }, 'params': {}, 'mono': dict.fromkeys(services, stamp),
      'received': dict.fromkeys(services, sender_time),
      'valid': dict.fromkeys(services, True), 'alive': dict.fromkeys(services, True),
    }))

  packet = relay_update(1200., 1199.98)
  fresh = source.update()
  left, right = fresh.lanes[1:3]
  assert not fresh.lanes[0].visible and not fresh.lanes[3].visible
  assert (left.style, left.color, left.visible) == ('solid', YELLOW, True)
  assert (right.style, right.color, right.visible) == ('dashed', BLUE, True)
  assert source.sm.logMonoTime['carState'] == 25_000_000_000
  assert source.sm.source_log_mono_time['carState'] == 1_200_000_000_000
  mesh_colors = {strip.color for strip in build_cluster_scene(fresh).lane_markings}
  assert {rgba(YELLOW), rgba(BLUE)} <= mesh_colors

  local_time += .3
  expired = source.update()
  assert all(marking.style == 'unknown' and not marking.visible for marking in expired.lanes[1:3])
  assert not {rgba(YELLOW), rgba(BLUE)} & {strip.color for strip in build_cluster_scene(expired).lane_markings}

  local_time += .7
  packet = relay_update(1201., 1199.98)
  stale = source.update()
  assert stale.lanes == expired.lanes
  assert [lane.model_points for lane in stale.lanes] == [lane.model_points for lane in fresh.lanes]

  local_time += 1.
  packet = relay_update(1202., 0.)
  old_sender = source.update()
  assert old_sender.lanes == stale.lanes

  local_time += 1.
  packet = relay_update(1203., 1203., left=12, right=10)
  one_side = source.update()
  assert not one_side.lanes[1].visible
  assert (one_side.lanes[2].style, one_side.lanes[2].color, one_side.lanes[2].visible) == ('dashed', WHITE, True)

  packet = None
  assert not source.update().lanes

  local_time += 1.
  packet = relay_update(1204., 0., fingerprint='KIA_EV6')
  other = source.update()
  assert (other.lanes[1].style, other.lanes[1].color, other.lanes[1].visible) == ('solid', YELLOW, True)
  assert (other.lanes[2].style, other.lanes[2].color, other.lanes[2].visible) == ('solid', BLUE, True)
