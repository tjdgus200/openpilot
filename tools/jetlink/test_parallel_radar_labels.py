import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from parallel_radar_labels import HEADER, OFFSETS, ParallelRadarLabels, WIDE_BYTES
from test_device_state import runtime as engine_runtime


class Detector:
  def __init__(self):
    self.entered = threading.Event()
    self.continue_run = threading.Event()
    self.closed = threading.Event()
    self.images = []
    self.fail = False

  def detect(self, image):
    self.images.append(image)
    self.entered.set()
    assert self.continue_run.wait(2)
    if self.fail:
      raise RuntimeError('synthetic late CUDA failure')
    return 1, bytes(24)

  def close(self):
    self.closed.set()


@pytest.fixture
def runtime():
  detector = Detector()
  output = queue.Queue()
  aux = ParallelRadarLabels(lambda: detector, lambda h, rows: output.put((h, rows)))
  assert aux.ready.wait(1)
  try:
    yield detector, aux, output
  finally:
    detector.continue_run.set()
    aux.close()
    assert detector.closed.wait(2)
    aux.thread.join(1)
    assert not aux.thread.is_alive()


def pair():
  image = np.zeros((2, 6, 128, 256), np.uint8)
  image[1].reshape(-1)[:] = np.arange(WIDE_BYTES, dtype=np.uint8)
  return image


def test_one_in_flight_owns_only_wide_bytes_and_requires_main_success(runtime):
  detector, aux, output = runtime
  warped = pair()
  expected = warped[1].tobytes()
  stamp = time.monotonic_ns()
  aux.offer(14, 3, stamp, warped)
  assert detector.entered.wait(1)
  warped.fill(255)
  for i in range(4, 24):
    aux.offer(i + 20, i, time.monotonic_ns(), warped)
  assert detector.images == [expected]
  detector.continue_run.set()
  aux.finish(99, True)
  assert not aux.sample.replied.is_set()
  aux.finish(14, True)
  header, rows = output.get(timeout=1)
  assert HEADER.unpack(header) == (b'RLD2', 3, stamp, bytes(expected[i] for i in OFFSETS), 1)
  assert len(rows) == 24


@pytest.mark.parametrize('cancel', ['failed', 'reset', 'close', 'late_error'])
def test_cancellation_does_not_free_inflight_gpu_resources(runtime, cancel):
  detector, aux, output = runtime
  aux.offer(11, 1, time.monotonic_ns(), pair())
  assert detector.entered.wait(1)
  aux.finish(11, cancel != 'failed')
  if cancel == 'reset':
    aux.invalidate()
  elif cancel == 'close':
    aux.close()
  elif cancel == 'late_error':
    detector.fail = True
  assert not detector.closed.is_set()
  detector.continue_run.set()
  assert aux.slot.acquire(timeout=1)
  aux.slot.release()
  assert output.empty()
  if cancel in ('close', 'late_error'):
    assert detector.closed.wait(1)
    assert not aux.ready.is_set()


def test_accepts_every_20hz_frame_without_phase_filter(runtime, monkeypatch):
  detector, aux, output = runtime
  detector.continue_run.set()
  image = pair()
  for frame in range(20):
    stamp = 1_000_000_000 + frame * 50_000_000
    monkeypatch.setattr('parallel_radar_labels.time.monotonic_ns', lambda: stamp)
    aux.offer(frame+100, frame, stamp, image)
    aux.finish(frame+100, True)
    assert HEADER.unpack(output.get(timeout=1)[0])[1:3] == (frame, stamp)
    assert aux.slot.acquire(timeout=1)
    aux.slot.release()
  assert len(detector.images) == 20


def test_stale_or_wrong_geometry_is_never_dispatched(runtime):
  detector, aux, _ = runtime
  warped = pair()
  now = time.monotonic_ns()
  aux.offer(1, 1, now - 50_000_001, warped)
  aux.offer(2, 2, now + 1_000_000_000, warped)
  aux.offer(3, 3, now, warped[0])
  assert aux.sample is None


def test_production_session_admits_only_negotiated_decoded_wide_frame():
  import server
  from jetlink import protocol as P

  calls = []
  labels = SimpleNamespace(offer=lambda *a: calls.append(a), invalidate=lambda: calls.append('reset'))
  session = SimpleNamespace(labels=labels, t=SimpleNamespace(protocol_version=2))
  engine = SimpleNamespace(label_device=0, run=lambda after_enqueue=None: (after_enqueue and after_enqueue(), 'main')[1])
  msg = SimpleNamespace(seq=9, received_ns=100)
  warped = pair()
  def run(flags):
    return server.CarrotSession._run_with_labels(session, engine, warped, 2, flags, msg)
  assert run(0) == 'main' and not calls
  session.t.protocol_version = 3
  assert run(P.WIDE_LABELS_FLAG) == 'main' and not calls
  session.t.protocol_version = 2
  assert run(P.WIDE_LABELS_FLAG | P.Flag.RESET_QUEUES) == 'main' and calls.pop() == 'reset'
  assert run(P.WIDE_LABELS_FLAG) == 'main'
  seq, frame, stamp, pixels = calls.pop()
  assert (seq, frame, stamp) == (9, 2, 100) and pixels is warped


def test_handoff_exception_does_not_skip_main_fence():
  import server
  from jetlink import protocol as P
  events = []
  def fail(*args):
    raise RuntimeError('synthetic handoff fault')
  def run(after_enqueue):
    events.append('enqueue')
    after_enqueue()
    events.append('fence')
    return 'main'
  session = SimpleNamespace(labels=SimpleNamespace(offer=fail, close=lambda: events.append('close')),
                            t=SimpleNamespace(protocol_version=2))
  result = server.CarrotSession._run_with_labels(session, SimpleNamespace(label_device=0, run=run), pair(),
                                                 2, P.WIDE_LABELS_FLAG, SimpleNamespace(seq=9, received_ns=100))
  assert result == 'main' and session.run_with_labels is None
  assert events == ['enqueue', 'close', 'fence']


@pytest.mark.parametrize('graph', [False, True])
@pytest.mark.parametrize('short_reply', [False, True])
def test_aux_offer_is_after_main_launch_before_only_main_fence(engine_runtime, graph, short_reply):
  rt = engine_runtime
  cuda, calls = rt.cuda, []
  cuda.graph_launch = lambda *_: calls.append('main graph')
  cuda.stream_sync = lambda *_: calls.append('main stream sync')
  cuda.event_sync = lambda *_: calls.append('main reply sync')
  engine = rt.engine.TrtEngine.__new__(rt.engine.TrtEngine)
  engine.stream, engine.graph_exec, engine.outputs = 1, (7 if graph else None), {}
  engine.device_state = SimpleNamespace(before_run=lambda: None, reply=3) if short_reply else None
  engine._enqueue = lambda: calls.append('main enqueue')
  assert engine.run(after_enqueue=lambda: calls.append('wide offer')) == {}
  assert calls == ['main graph' if graph else 'main enqueue', 'wide offer',
                   'main reply sync' if short_reply else 'main stream sync']


@pytest.mark.parametrize('compressed', [False, True])
def test_validated_session_frame_is_identical_for_both_camera_encodings(compressed):
  from jetlink import block_codec, protocol as P
  from jetlink.server.session import Session

  frames, replies = [], []
  image = pair()
  flags = P.WIDE_LABELS_FLAG | (block_codec.FLAG if compressed else 0)
  spec = SimpleNamespace(stateful=True, warped_nbytes=image.nbytes, warped_shape=image.shape,
                         packed_nbytes=4, packed_nelem=1)
  def run_with_labels(engine, warped, frame, got_flags, msg):
    frames.append((frame, got_flags, bytes(warped[1])))
    return {'outputs': np.zeros(1, dtype=np.float32)}
  session = SimpleNamespace(t=SimpleNamespace(protocol_version=2), frames=0, hud_inference_timing=False,
                            _resolve_context=lambda spec, flags, ctx: (flags, ctx), run_with_labels=run_with_labels,
                            _send=lambda kind, seq, parts: replies.append(P.unpack_infer_resp(parts[0])[1]))
  queues = SimpleNamespace(step_into=lambda *args: None, feedback=lambda _: None)
  loaded = SimpleNamespace(spec=spec, queues=queues, host_inputs={}, engine=SimpleNamespace(last_gpu_us=0))
  body = image.tobytes() + bytes(4)
  if compressed:
    body = block_codec.encode([body])
    if body is None:
      pytest.skip('liblz4 unavailable')
  header = P.pack_infer_req(3, flags)
  Session._infer(session, loaded, SimpleNamespace(seq=9, payload=memoryview(header + body)))
  Session._infer(session, loaded, SimpleNamespace(seq=10, payload=memoryview(header + body[:10])))
  assert frames == [(3, flags, image[1].tobytes())]
  assert replies == [P.Status.OK, P.Status.BAD_SHAPE]
