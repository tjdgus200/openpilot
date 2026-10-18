"""Exact CPU staging and serial-state tests; no real TensorRT/USB access."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'third_party/jetlink'), str(Path(__file__).parent)]
from jetlink import protocol as P
from jetlink.input_cast import native_u8_to_f16
from jetlink.packed_codec import FLAG, _layout, decode, encode
from jetlink.queues import make_queues
from jetlink.server.session import EngineHost, Session
from jetlink.spec import ModelSpec

SPEC = ModelSpec.from_dict(json.loads((ROOT / 'openpilot/selfdrive/modeld/jetlink/jetson_model.json').read_text()))


@pytest.fixture
def native():
  value = native_u8_to_f16()
  if value is None:
    pytest.skip('Native C compiler unavailable on this platform')
  return value


def bindings(dtype=np.float16):
  return {name: np.zeros(shape, dtype) for name, shape in SPEC.input_shapes.items()}


@pytest.mark.parametrize('length', [*range(34), 63, 64, 65, 255, 256, 257, 1023, 65536, 393216])
def test_native_conversion_is_bit_exact_and_respects_destination_canaries(native, length):
  source = np.arange(length + 2, dtype=np.uint8)[1:-1]
  output = np.full(length + 2, 0x3555, np.uint16)
  native(source, output[1:-1].view(np.float16))
  assert output[0] == output[-1] == 0x3555
  assert output[1:-1].tobytes() == source.astype(np.float16).tobytes()


@pytest.mark.parametrize('fault', ['source_type', 'dest_type', 'size', 'stride', 'readonly', 'unaligned', 'alias'])
def test_native_refuses_unsafe_buffers_without_writing(native, fault):
  source, dest = np.arange(16, dtype=np.uint8), np.full(16, 12, np.float16)
  if fault == 'source_type':
    source = source.astype(np.int8)
  elif fault == 'dest_type':
    dest = dest.astype(np.float32)
  elif fault == 'size':
    source = source[:-1]
  elif fault == 'stride':
    source = np.arange(32, dtype=np.uint8)[::2]
  elif fault == 'readonly':
    dest.flags.writeable = False
  elif fault == 'unaligned':
    dest = np.frombuffer(bytearray(33), np.float16, count=16, offset=1)
  else:
    source = dest.view(np.uint8)[:16]
  before = dest.tobytes()
  with pytest.raises(ValueError):
    native(source, dest)
  assert dest.tobytes() == before


@pytest.mark.parametrize('dtype', [np.float16, np.float32])
def test_compact_and_raw_inputs_match_all_binding_bits_over_changing_frames(native, dtype):
  old, new = bindings(dtype), bindings(dtype)
  baseline = make_queues(SPEC, old)
  candidate = make_queues(SPEC, new, input_fastpath=True)
  rng = np.random.default_rng(20261019)
  at = _layout(SPEC) // 4
  for index in range(64):
    image = rng.integers(0, 256, SPEC.warped_shape, np.uint8)
    packed = rng.uniform(-3, 3, SPEC.packed_nelem).astype(np.float32)
    packed[0] = -0.0
    packed[at:at + 65536] = 0 if index % 5 == 0 else rng.integers(0, 256, 65536, np.uint8)
    payload, used = encode(packed, SPEC)
    assert used
    baseline.step_into(image, np.frombuffer(decode(payload, SPEC), np.float32), old)
    if index % 2:
      candidate.step_into(image, packed, new)
    else:
      candidate.step_compact_into(image, payload, new)
    for name in old:
      assert old[name].tobytes() == new[name].tobytes(), (index, name)


@pytest.mark.parametrize('at', [0, 7, 8, 11, 65548, 65594])
@pytest.mark.parametrize('bad', [np.nan, np.inf, -np.inf])
def test_nonfinite_compact_scalars_reset_all_history_before_copying(native, at, bad):
  dest = bindings()
  queues = make_queues(SPEC, dest, input_fastpath=True)
  for state in queues.state.values():
    state.fill(3)
  packed = np.zeros(SPEC.packed_nelem, np.float32)
  packed[at] = bad
  payload, used = encode(packed, SPEC)
  assert used  # Bad scalar, not a map pixel; wire codec preserves it.
  dest['new_img'].fill(7)
  with pytest.raises(ValueError, match='Non-finite'):
    queues.step_compact_into(np.zeros(SPEC.warped_shape, np.uint8), payload, dest)
  assert np.all(dest['new_img'] == 7)
  assert all(not np.any(value) for value in queues.state.values())


@pytest.mark.parametrize('fault', ['not_enabled', 'missing_native', 'binding_dtype', 'binding_readonly', 'binding_shape', 'changed_buffers'])
def test_opt_in_is_explicit_and_rejects_incompatible_bindings(native, monkeypatch, fault):
  dest = bindings()
  if fault == 'missing_native':
    monkeypatch.setattr('jetlink.direct_inputs.native_u8_to_f16', lambda: None)
    with pytest.raises(RuntimeError, match='unavailable'):
      make_queues(SPEC, dest, input_fastpath=True)
    return
  if fault.startswith('binding_'):
    if fault == 'binding_dtype':
      dest['nav_map'] = dest['nav_map'].astype(np.float64)
    elif fault == 'binding_readonly':
      dest['nav_map'].flags.writeable = False
    else:
      dest['nav_map'] = dest['nav_map'].reshape(-1)
    with pytest.raises(ValueError, match='binding'):
      make_queues(SPEC, dest, input_fastpath=True)
    return
  queues = make_queues(SPEC, dest, input_fastpath=fault != 'not_enabled')
  payload, _ = encode(np.zeros(SPEC.packed_nelem, np.float32), SPEC)
  with pytest.raises((RuntimeError, ValueError)):
    queues.step_compact_into(np.zeros(SPEC.warped_shape, np.uint8), payload, dict(dest))


def mock_session(enabled, dtype=np.float16):
  dest = bindings(dtype)
  engine = NS(inputs={n: NS(shape=s) for n, s in SPEC.input_shapes.items()},
              outputs={n: NS(shape=s) for n, s in SPEC.output_shapes.items()},
              host_input=dest.__getitem__, warm=lambda: 'mock only', last_gpu_us=1, input_fastpath=enabled)
  captured = []
  def run():
    # All graph inputs, including accumulated state, affect the mock output.
    value = sum(float(value.reshape(-1)[:16].sum()) for value in dest.values()) % 16
    captured.append({n: hashlib.sha256(memoryview(value).cast('B')).digest() for n, value in dest.items()})
    return {**{out: (dest[name] + 1).astype(dest[name].dtype) for name, out in SPEC.state_pairs.items()},
            'outputs': np.full(SPEC.output_shapes['outputs'], value, np.float32)}
  engine.run = run
  loaded = EngineHost._warm(None, engine, SPEC)
  replies = []
  transport = NS(protocol_version=2, send=lambda kind, seq, parts, flags: replies.append(
    b''.join(memoryview(part).cast('B') for part in parts)))
  host = NS(telemetry=NS(read=dict), frame_stats=NS(record=lambda *a: None))
  return Session(transport, host), loaded, replies, captured


@pytest.mark.parametrize('version', [2, 3])
def test_full_session_preserves_wire_results_resets_and_state_with_raw_and_compact_inputs(native, version):
  references = [mock_session(False), mock_session(True)]
  for session, *_ in references:
    session.t.protocol_version = version
  rng = np.random.default_rng(20261019)
  for index in range(40):
    packed = rng.uniform(-2, 2, SPEC.packed_nelem).astype(np.float32)
    packed[12:12 + 65536] = rng.integers(0, 256, 65536, np.uint8)
    payload, used = encode(packed, SPEC)
    assert used
    flags = P.Flag.RESET_QUEUES if index in (0, 7, 29) else 0
    if index % 3 and version == 2:
      flags |= FLAG
    else:
      payload = packed.tobytes()
    request = P.pack_infer_req(index, flags) + rng.integers(0, 256, SPEC.warped_nbytes, np.uint8).tobytes() + payload
    for session, loaded, replies, _captured in references:
      session._infer(loaded, NS(seq=index, payload=memoryview(request)))
      assert P.unpack_infer_resp(memoryview(replies[-1]))[:2] == (index, P.Status.OK)
    assert references[0][2][-1][P.INFER_RESP_SIZE:] == references[1][2][-1][P.INFER_RESP_SIZE:]
    assert references[0][3][-1] == references[1][3][-1]


@pytest.mark.parametrize('version,delta', [(2, -1), (2, 1), (2, 65536), (3, 0)])
def test_bad_compact_wire_frame_never_runs_the_engine(native, version, delta):
  session, loaded, replies, seen = mock_session(True)
  session.t.protocol_version = version
  compact, _ = encode(np.zeros(SPEC.packed_nelem, np.float32), SPEC)
  payload = P.pack_infer_req(4, FLAG) + bytes(SPEC.warped_nbytes) + bytes(len(compact) + delta)
  session._infer(loaded, NS(seq=4, payload=memoryview(payload)))
  assert P.unpack_infer_resp(memoryview(replies[-1]))[1] == P.Status.BAD_SHAPE
  assert not seen


def test_default_backend_keeps_legacy_inputs_and_does_not_load_new_converter(monkeypatch):
  monkeypatch.setattr('jetlink.direct_inputs.native_u8_to_f16', lambda: pytest.fail('compiler loaded by default path'))
  queues = make_queues(SPEC, bindings())
  assert queues._fast_inputs is None
  # A generic engine without the TRT-only attribute remains unchanged.
  _, loaded, _, _ = mock_session(False)
  assert loaded.queues._fast_inputs is None


@pytest.mark.parametrize('mutation', ['replacement', 'shape', 'dtype', 'readonly'])
def test_live_binding_changes_are_rejected_before_any_image_is_written(native, mutation):
  dest = bindings()
  queues = make_queues(SPEC, dest, input_fastpath=True)
  payload, _ = encode(np.zeros(SPEC.packed_nelem, np.float32), SPEC)
  dest['new_img'].fill(7)
  if mutation == 'replacement':
    dest['nav_map'] = dest['nav_map'].copy()
  elif mutation == 'shape':
    dest['nav_map'].shape = (65536,)
  elif mutation == 'dtype':
    dest['nav_map'].dtype = np.uint16
  else:
    dest['nav_map'].flags.writeable = False
  with pytest.raises(ValueError, match='changed after load'):
    queues.step_compact_into(np.zeros(SPEC.warped_shape, np.uint8), payload, dest)
  assert np.all(dest['new_img'] == 7)


@pytest.mark.parametrize('failure', ['missing_compiler', 'compiler_failure'])
def test_native_compilation_failure_cannot_produce_a_converter(monkeypatch, failure):
  import subprocess
  from jetlink import input_cast
  if failure == 'missing_compiler':
    monkeypatch.setattr(input_cast.shutil, 'which', lambda name: None)
  else:
    def fail(*args, **kwargs):
      raise subprocess.CalledProcessError(1, 'cc')
    monkeypatch.setattr(input_cast.subprocess, 'run', fail)
  assert native_u8_to_f16.__wrapped__() is None


def test_lz4_and_compact_flags_preserve_input_and_output_bits_together(native):
  from jetlink import block_codec
  if block_codec.library() is None:
    pytest.skip('Optional LZ4 library unavailable')
  image = np.zeros(SPEC.warped_nbytes, np.uint8)
  packed = np.zeros(SPEC.packed_nelem, np.float32)
  compact, _ = encode(packed, SPEC)
  block = block_codec.encode((image, compact))
  assert block is not None
  references = [mock_session(False), mock_session(True)]
  payload = P.pack_infer_req(1, FLAG | block_codec.FLAG | P.Flag.RESET_QUEUES) + block
  for session, loaded, replies, _ in references:
    session._infer(loaded, NS(seq=1, payload=memoryview(payload)))
    assert P.unpack_infer_resp(memoryview(replies[-1]))[:2] == (1, P.Status.OK)
  assert references[0][3] == references[1][3]
  assert references[0][2][-1][P.INFER_RESP_SIZE:] == references[1][2][-1][P.INFER_RESP_SIZE:]


@pytest.fixture
def deferred_cuda(monkeypatch):
  from test_device_state_port import runtime
  return runtime.__wrapped__(monkeypatch)


def test_device_recurrence_order_and_failure_checks_are_unchanged(native, monkeypatch, deferred_cuda):
  # Reuse the deferred CUDA fixture: tests ordering, not Orin CUDA performance.
  from test_device_state_port import SPEC as tiny_spec, build
  outputs = []
  for enabled in (False, True):
    engine, queues, dest = build(deferred_cuda, monkeypatch, True)
    if enabled:
      queues.configure_input_fastpath(dest)
    sequence = []
    for frame in range(32):
      if frame in (5, 23):
        queues.reset()
      queues.step_into(np.full(tiny_spec.warped_shape, frame % 7, np.uint8), np.zeros(1, np.float32), dest)
      result = engine.run()
      queues.feedback(result)
      sequence.append(result['outputs'].tobytes())
    engine.inject = 'next_img'
    result = engine.run()
    with pytest.raises(FloatingPointError):
      queues.feedback(result)
    engine.inject = None
    queues.feedback(engine.run())
    with pytest.raises(ValueError, match='Non-finite'):
      queues.step_into(np.zeros(tiny_spec.warped_shape, np.uint8), np.array([np.inf], np.float32), dest)
    sequence.append(engine.run()['outputs'].tobytes())
    engine.close()
    outputs.append(sequence)
  assert outputs[0] == outputs[1]
