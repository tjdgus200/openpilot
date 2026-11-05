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
