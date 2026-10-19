import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import vision_cpu as worker
from openpilot.selfdrive.carrot.xiaoge import remote as r
from openpilot.selfdrive.carrot.xiaoge.xiaoge_vision import XiaogeVisionResult, apply_xiaoge_vision_result


def request(kind='lane', **extra):
  metadata = dict(version=1, id='a'*32, kind=kind, threshold=.25, sha256='b'*64)
  metadata.update(extra)
  meta = json.dumps(metadata).encode()
  return r.PREFIX.pack(len(meta)) + meta + np.zeros(r.SHAPES[kind], np.uint8).tobytes()


@pytest.mark.parametrize('kind', ['lane', 'vasm'])
def test_lossless_request_size_and_image(kind):
  data = request(kind)
  meta, image = r.decode_request(data)
  assert meta['kind'] == kind and set(image) == {0}
  assert len(data) < r.MAX_REQUEST


@pytest.mark.parametrize('change', [dict(threshold=True), dict(threshold=float('nan')),
                                   dict(threshold=-1), dict(id='short'), dict(version=2)])
def test_request_rejects_invalid_metadata(change):
  with pytest.raises(ValueError):
    r.decode_request(request(**change))


def test_short_or_overlong_image_rejected():
  for data in (request()[:-1], request() + b'\0'):
    with pytest.raises(ValueError):
      r.decode_request(data)


def test_frame_length_checked_before_allocating():
  a, b = socket.socketpair()
  with a, b:
    a.sendall(r.PREFIX.pack(2**31))
    with pytest.raises(ValueError):
      r.read_record(b, r.MAX_REQUEST, time.monotonic() + .1)


@pytest.mark.parametrize('corruption', ['identity', 'model', 'processor', 'invalid', 'none'])
def test_client_uses_local_deadline_and_rejects_incompatible_results(tmp_path, monkeypatch, corruption):
  path = tmp_path / 'rpc'
  monkeypatch.setattr(r, 'SOCKET', str(path))
  client = r.CpuClient('lane', tmp_path / 'model')
  client.model_path.write_bytes(b'synthetic model')
  assert client.load()
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(str(path))
  listener.listen(1)
  def respond():
    with listener:
      sock, _ = listener.accept()
      with sock:
        data = r.read_record(sock, r.MAX_REQUEST, time.monotonic() + 1)
        meta, raw = r.decode_request(data)
        assert raw == bytes(416 * 416)
        result = dict(id=meta['id'], sha256=client.sha256, processor='jetson_cpu_wifi', valid=True)
        if corruption == 'identity': result['id'] = 'x'*32
        if corruption == 'model': result['sha256'] = '0'*64
        if corruption == 'processor': result['processor'] = 'other'
        if corruption == 'invalid': result['valid'] = False
        r.send_record(sock, json.dumps(result).encode(), time.monotonic() + 1)
  thread = threading.Thread(target=respond)
  thread.start()
  try:
    if corruption == 'none':
      assert client.infer(np.zeros((416, 416), np.uint8))['valid']
    else:
      with pytest.raises(ValueError): client.infer(np.zeros((416, 416), np.uint8))
  finally:
    thread.join(2)
  assert not thread.is_alive()


def vision(left=0, right=0, **kw):
  values = dict(left_lane=left, right_lane=right, lane_valid=True, lane_received_nanos=10_000_000_000,
                left_blindspot=False, right_blindspot=False, blindspot_valid=True,
                blindspot_received_nanos=10_000_000_000, left_blindspot_received_nanos=10_000_000_000,
                right_blindspot_received_nanos=10_000_000_000, remote_cpu=True,
                left_evaluated=True, right_evaluated=True)
  values.update(kw)
  return XiaogeVisionResult(**values)


def state(left=10, right=10, tracked=True, valid=True):
  return NS(leftLaneLine=left, rightLaneLine=right, leftBlindspot=False, rightBlindspot=False,
            laneTypeSources=NS(oemTracked=tracked, leftOemValid=valid, rightOemValid=valid,
                               oemAvailable=tracked, oemDiscoveryComplete=True,
                               leftOem=left, rightOem=right, visionRequired=True))


@pytest.mark.parametrize('oem', [11, 13, 14, 18, 21, 28])
def test_onnx_dashed_never_clears_oem_solid_or_boundary(oem):
  cs = state(oem)
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert cs.leftLaneLine == oem and cs.laneTypeSources.leftAutoHold


def test_disagreement_blocks_automatic_and_keeps_vehicle_color():
  cs = state(20)
  apply_xiaoge_vision_result(cs, vision(left=1), 10_000_000_001)
  assert cs.leftLaneLine == 21 and cs.laneTypeSources.leftAutoHold
  assert not cs.laneTypeSources.rightAutoHold


def test_confirmed_dashed_and_checked_clear_is_only_additional_veto_release():
  cs = state()
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert cs.leftLaneLine == 10 and not cs.laneTypeSources.leftAutoHold
  # OEM BSD is never cleared by a remote false result.
  cs.leftBlindspot = True
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert cs.leftBlindspot


@pytest.mark.parametrize('result', [None, vision(lane_valid=False), vision(remote_cpu=False),
                                 vision(left=-1), vision(left_evaluated=False), vision(left_blindspot=True),
                                 vision(left_blindspot_received_nanos=1), vision(lane_received_nanos=1),
                                 vision(lane_received_nanos=11_000_000_000)])
def test_missing_unknown_unchecked_stale_and_future_cannot_grant_automatic_change(result):
  cs = state()
  apply_xiaoge_vision_result(cs, result, 10_000_000_001)
  assert cs.laneTypeSources.leftAutoHold


def test_unknown_stock_zero_uses_fresh_onnx_without_claiming_oem_support():
  cs = state(0, tracked=False, valid=False)
  apply_xiaoge_vision_result(cs, vision(left=1), 10_000_000_001)
  assert cs.leftLaneLine == 1 and cs.laneTypeSources.leftAutoHold
  apply_xiaoge_vision_result(cs, None, 10_000_000_002)
  assert cs.leftLaneLine == 1  # explicit solid retained, never converted to dashed
  assert cs.laneTypeSources.leftAutoHold


def test_expired_tracked_stock_must_not_be_reused_as_fresh():
  cs = state(10, valid=False)
  apply_xiaoge_vision_result(cs, None, 10_000_000_001)
  assert cs.leftLaneLine == -1 and cs.laneTypeSources.leftAutoHold


def test_capable_vehicle_undecided_cannot_be_replaced_with_dashed_permission():
  cs = state(12, valid=False)
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert cs.laneTypeSources.leftAutoHold
  cs.laneTypeSources.oemAvailable = False
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert not cs.laneTypeSources.leftAutoHold
  cs.laneTypeSources.oemDiscoveryComplete = False
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert cs.laneTypeSources.leftAutoHold


def test_legacy_colored_undecided_native_type_cannot_grant_automatic_start():
  cs = state(12, tracked=False, valid=False)
  apply_xiaoge_vision_result(cs, vision(), 10_000_000_001)
  assert cs.laneTypeSources.leftAutoHold


@pytest.mark.parametrize('kind', ['lane', 'vasm'])
def test_existing_cpu_model_results_match_lossless_rpc_input(kind):
  import hashlib
  import cv2
  from openpilot.selfdrive.carrot.xiaoge.lane_inference import LaneInference
  from openpilot.selfdrive.carrot.xiaoge.v_asm_inference import VASMInference
  cv2.setNumThreads(1)
  path = worker.ROOT / 'openpilot/selfdrive/carrot/xiaoge/assets' / r.FILES[kind]
  digest = hashlib.sha256(path.read_bytes()).hexdigest()
  image = (np.arange(np.prod(r.SHAPES[kind]), dtype=np.uint32) * 17 % 256).astype(np.uint8).reshape(r.SHAPES[kind])
  metadata = json.dumps(dict(version=1, id='a'*32, kind=kind, sha256=digest, threshold=.25)).encode()
  result = json.loads(worker.Engine().infer(r.PREFIX.pack(len(metadata)) + metadata + image.tobytes()))
  reference = (LaneInference if kind == 'lane' else VASMInference)(path)
  assert reference.load()
  reference.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
  reference.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
  if kind == 'lane':
    expected = reference.infer(image, 416, 416)
    for name in ('leftLine','rightLine','leftConf','rightConf','valid','candidatesCount'):
      assert result[name] == expected[name]
  else:
    assert result['confidence'] == reference.predict_square(image)


@pytest.mark.parametrize('direction', ['left', 'right'])
@pytest.mark.parametrize('held', [True, False])
def test_actual_lane_change_fsm_respects_side_specific_veto(direction, held):
  from openpilot.selfdrive.controls.tests.test_desire_helper_stalk import TestDesireHelperStalkRequest, LCS
  # Reuse the stock FSM fixture, not an imitation of the transition.
  fixture = TestDesireHelperStalkRequest()
  fixture.setup_method()
  helper = fixture.helper
  car = NS(canValid=True, leftBlinker=False, rightBlinker=False, vEgo=25., aEgo=0.,
           trailerConnected=False, steeringTorque=0., steeringPressed=False, blinkerLever=0,
           laneTypeSources=NS(visionRequired=True, leftAutoHold=held if direction == 'left' else True,
                              rightAutoHold=held if direction == 'right' else True))
  def tick():
    helper.update(car, NS(), True, .5, fixture.carrot_man, NS())
  tick()
  setattr(car, direction + 'Blinker', True)
  tick()
  assert helper.lane_change_state == LCS.preLaneChange
  tick()
  assert helper.lane_change_state == (LCS.preLaneChange if held else LCS.laneChangeStarting)
  if held:
    # Restoring a fresh checked result releases only this veto, with the
    # normal geometry/BSD/start conditions still evaluated by the FSM.
    setattr(car.laneTypeSources, direction + 'AutoHold', False)
    tick()
    assert helper.lane_change_state == LCS.laneChangeStarting
  setattr(car.laneTypeSources, direction + 'AutoHold', True)
  tick()
  assert helper.lane_change_state == LCS.laneChangeStarting


def test_encrypted_roundtrip_and_sequence_binding(monkeypatch):
  # Actual host receive loop on socketpair, no CAN, GPU, camera or real network.
  a, b = socket.socketpair()
  runtime = worker.Runtime('receive', bytes(32), None)
  runtime.engine = NS(infer=lambda data: b'{"valid":true}')
  errors = []
  def serve():
    try:
      with b: runtime.receive(b)
    except (OSError, ValueError) as error:
      errors.append(type(error).__name__)
  thread = threading.Thread(target=serve)
  thread.start()
  with a:
    _, key = worker.authenticate(a, runtime.key, 1)
    header = r.PREFIX.pack(1)
    r.send_record(a, header + worker.seal(key, 1, 0, header, request()), time.monotonic() + 1)
    result = r.read_record(a, r.MAX_RESPONSE, time.monotonic() + 1)
    assert worker.unseal(key, 1, 1, header, result) == b'{"valid":true}'
    # Repeated sequence must close, not run another frame.
    r.send_record(a, header + worker.seal(key, 1, 0, header, request()), time.monotonic() + 1)
  thread.join(2)
  assert errors == ['ValueError'] and not thread.is_alive()
