import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from hud_yolo_protocol import HEADER as PREVIEW_HEADER, MAGIC, MAX_AGE_NS, MAX_BOXES, ROW, SnapshotReader, read_snapshot, write_snapshot
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
    return 1, ROW.pack(2, .75, 256., 64., 512., 170.)

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


def test_one_in_flight_owns_only_wide_bytes_and_requires_main_success(runtime, monkeypatch):
  detector, aux, output = runtime
  clock = iter((1., 1.004))
  monkeypatch.setattr('parallel_radar_labels.time.perf_counter', lambda: next(clock, 2.))
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
  assert aux.last_detection == (1., 1.004)
  aux.invalidate()
  assert aux.last_detection is None


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
  assert aux.last_detection is None
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


def test_preview_is_exact_successful_input_independent_of_radar_and_removed_on_yolo_off(tmp_path, monkeypatch):
  detector = Detector()
  detector.continue_run.set()
  path = tmp_path / 'preview.packet'

  def radar_unavailable(header, rows):
    raise OSError('no association receiver')

  aux = ParallelRadarLabels(lambda: detector, radar_unavailable, preview_path=path)
  assert aux.ready.wait(1)
  try:
    def submit(seq, stamp, valid, preview):
      monkeypatch.setattr('parallel_radar_labels.time.monotonic_ns', lambda: stamp)
      aux.set_request(True, preview)
      aux.offer(seq, seq + 10, stamp, pair())
      aux.finish(seq, valid)
      assert aux.slot.acquire(timeout=1)
      aux.slot.release()

    stamp = 1_000_000_000
    submit(1, stamp, True, False)
    assert not path.exists()
    submit(2, stamp + 50_000_000, False, True)
    assert not path.exists()
    submit(3, stamp + 100_000_000, True, True)
    preview = read_snapshot(path)
    assert preview.frame_id == 13 and preview.stamp_ns == stamp + 100_000_000
    assert preview.image == pair()[1].tobytes()
    assert preview.rows == ((2, .75, 256., 64., 512., 170.),)
    submit(4, stamp + 150_000_000, True, True)
    assert read_snapshot(path) == preview  # Unchanged inside the 10 Hz budget.

    aux.set_request(False, True)  # A stray preview bit must never enable YOLO.
    aux.offer(5, 15, time.monotonic_ns(), pair())
    assert aux.sample is None and len(detector.images) == 4
    for _ in range(100):
      if not path.exists():
        break
      time.sleep(.001)
    assert not path.exists()
  finally:
    aux.close()
    aux.thread.join(1)


def test_yolo_off_cancels_a_queued_sample_even_after_reenable(tmp_path):
  detector = Detector()
  starting = threading.Event()
  continue_open = threading.Event()

  def open_detector():
    starting.set()
    assert continue_open.wait(1)
    return detector

  aux = ParallelRadarLabels(open_detector, lambda *_: None, preview_path=tmp_path / 'preview.packet')
  try:
    assert starting.wait(1)
    aux.ready.set()  # Queue a sample while the auxiliary worker cannot dispatch.
    aux.set_request(True, True)
    aux.offer(1, 2, time.monotonic_ns(), pair())
    pending = aux.sample
    assert pending is not None
    aux.set_request(False, False)
    aux.set_request(True, True)
    aux.finish(1, True)
    continue_open.set()
    assert aux.slot.acquire(timeout=1)
    aux.slot.release()
    assert not pending.valid and not detector.images
  finally:
    detector.continue_run.set()
    continue_open.set()
    aux.close()
    aux.thread.join(1)


def test_preview_reader_caches_fresh_identity_and_rejects_stale_or_invalid_frames(tmp_path):
  path = tmp_path / 'preview.packet'
  reader = SnapshotReader(path)
  stamp = 1_000_000_000
  image = bytes(WIDE_BYTES)
  write_snapshot(path, 1, stamp, 0, b'', image)
  first = reader.read(stamp)
  assert first.frame_id == 1 and first.rows == () and first.image == image
  assert reader.read(stamp + MAX_AGE_NS - 1) is first
  assert reader.read(stamp + MAX_AGE_NS) is None
  assert reader.read(stamp - 1) is None

  row = ROW.pack(7, .5, 1., 2., 3., 4.)
  write_snapshot(path, 2, stamp, 1, row, image)
  assert reader.read(stamp).rows == ((7, .5, 1., 2., 3., 4.),)
  for body in (PREVIEW_HEADER.pack(MAGIC, 3, stamp, MAX_BOXES + 1) + row + image,
               PREVIEW_HEADER.pack(MAGIC, 3, stamp, 1) + row + image[:-1],
               PREVIEW_HEADER.pack(MAGIC, 3, stamp, 1) + row + image + b'0',
               PREVIEW_HEADER.pack(b'FAIL', 3, stamp, 1) + row + image,
               PREVIEW_HEADER.pack(MAGIC, 3, stamp, 1) + ROW.pack(1, float('nan'), 1., 2., 3., 4.) + image,
               PREVIEW_HEADER.pack(MAGIC, 3, stamp, 1) + ROW.pack(9, .5, 1., 2., 3., 4.) + image,
               PREVIEW_HEADER.pack(MAGIC, 3, stamp, 1) + ROW.pack(1, .5, 2., 2., 1., 4.) + image):
    path.write_bytes(body)
    assert reader.read(stamp) is None
  path.unlink()
  assert reader.read(stamp) is None


def test_production_session_admits_only_negotiated_decoded_wide_frame():
  import server
  from jetlink import protocol as P

  calls, requests = [], []
  labels = SimpleNamespace(offer=lambda *a: calls.append(a), invalidate=lambda: calls.append('reset'),
                           set_request=lambda *a: requests.append(a))
  session = SimpleNamespace(labels=labels, t=SimpleNamespace(protocol_version=2))
  engine = SimpleNamespace(label_device=0, run=lambda after_enqueue=None: (after_enqueue and after_enqueue(), 'main')[1])
  msg = SimpleNamespace(seq=9, received_ns=100)
  warped = pair()
  def run(flags):
    return server.CarrotSession._run_with_labels(session, engine, warped, 2, flags, msg)
  assert run(0) == 'main' and not calls
  assert requests.pop() == (False, False)
  assert run(P.WIDE_YOLO_PREVIEW_FLAG) == 'main' and not calls
  assert requests.pop() == (False, True)
  session.t.protocol_version = 3
  assert run(P.WIDE_LABELS_FLAG) == 'main' and not calls
  assert requests.pop() == (False, False)
  session.t.protocol_version = 2
  assert run(P.WIDE_LABELS_FLAG | P.Flag.RESET_QUEUES) == 'main' and calls.pop() == 'reset'
  assert run(P.WIDE_LABELS_FLAG) == 'main'
  assert requests.pop() == (True, False)
  seq, frame, stamp, pixels = calls.pop()
  assert (seq, frame, stamp) == (9, 2, 100) and pixels is warped
  assert run(P.WIDE_LABELS_FLAG | P.WIDE_YOLO_PREVIEW_FLAG) == 'main'
  assert requests.pop() == (True, True)


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
  session = SimpleNamespace(labels=SimpleNamespace(offer=fail, close=lambda: events.append('close'), set_request=lambda *_: None),
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
