"""Bit-exact staging and unchanged reset/validation contracts."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'third_party/jetlink'), str(Path(__file__).parent)]
from jetlink.input_cast import native_u8_to_f16
from jetlink.queues import make_queues
from jetlink.packed_codec import encode, decode, FLAG
from openpilot.selfdrive.modeld.jetlink.contracts import JETSON_SPEC as SPEC
from jetlink import protocol as P, block_codec
from jetlink.server.session import EngineHost, Session


def buffers(dtype):
  return {name: np.zeros(shape, dtype) for name, shape in SPEC.input_shapes.items()}


@pytest.mark.parametrize('dtype', [np.float16, np.float32])
def test_compact_bits_match_float_decoder_and_preserve_state(dtype):
  a, b = buffers(dtype), buffers(dtype)
  baseline, candidate = make_queues(SPEC, a), make_queues(SPEC, b, input_fastpath=True)
  rng = np.random.default_rng(20261020)
  for index in range(6):
    pixels = rng.integers(0, 256, SPEC.warped_shape, dtype=np.uint8)
    packed = rng.uniform(-2, 2, SPEC.packed_nelem).astype(np.float32)
    packed[0] = -0.
    packed[12:12+65536] = np.arange(65536, dtype=np.uint8)
    payload, compact = encode(packed, SPEC)
    assert compact
    for q in (baseline, candidate):
      for value in q.state.values():value.fill(index)
    baseline.step_into(pixels, np.frombuffer(decode(payload, SPEC), np.float32), a)
    candidate.step_compact_into(pixels, payload, b)
    for name in SPEC.input_shapes:assert a[name].tobytes() == b[name].tobytes()
  baseline.reset();candidate.reset()
  assert all(not value.any() for q in (baseline, candidate) for value in q.state.values())


def test_nonfinite_compact_resets_state_before_copy():
  dest = buffers(np.float16);q = make_queues(SPEC, dest, input_fastpath=True)
  packed = np.zeros(SPEC.packed_nelem, np.float32)
  payload, _ = encode(packed, SPEC);payload = bytearray(payload)
  payload[:4] = np.float32(np.nan).tobytes()
  for value in q.state.values():value.fill(1)
  with pytest.raises(ValueError, match='Non-finite'):
    q.step_compact_into(np.zeros(SPEC.warped_shape,np.uint8), payload, dest)
  assert all(not value.any() for value in q.state.values())


def test_changed_binding_refused():
  dest=buffers(np.float16);q=make_queues(SPEC,dest,input_fastpath=True)
  payload,_=encode(np.zeros(SPEC.packed_nelem,np.float32),SPEC)
  dest['new_img']=dest['new_img'].copy()
  with pytest.raises(ValueError,match='buffers changed'):
    q.step_compact_into(np.zeros(SPEC.warped_shape,np.uint8),payload,dest)


def test_native_conversion_all_bytes_and_misaligned_source():
  convert=native_u8_to_f16();assert convert is not None
  for n in (0,1,7,8,15,16,31,32,255,256,257,1025):
    source=np.arange(n+1,dtype=np.uint8)[1:];dest=np.empty(n,np.float16)
    convert(source,dest)
    assert dest.tobytes()==source.astype(np.float16).tobytes()
  with pytest.raises(ValueError):convert(np.arange(8,dtype=np.uint8),np.empty(4,np.float16))
  with pytest.raises(ValueError):convert(np.arange(8,dtype=np.uint8)[::2],np.empty(4,np.float16))


def test_requested_fastpath_does_not_silently_fall_back(monkeypatch):
  import jetlink.direct_inputs as direct
  monkeypatch.setattr(direct,'native_u8_to_f16',lambda:None)
  with pytest.raises(RuntimeError,match='unavailable'):
    make_queues(SPEC,buffers(np.float16),input_fastpath=True)


@pytest.mark.parametrize('compressed', [False, True])
def test_session_fastpath_keeps_wire_reset_and_rejection_contracts(monkeypatch, compressed):
  if compressed and block_codec.library() is None:
    pytest.skip('System liblz4 unavailable')
  monkeypatch.setenv('CARROT_TRT_INPUT_FASTPATH', '1')
  dest = buffers(np.float16)
  seen = []
  def run():
    seen.append({name: value.copy() for name, value in dest.items()})
    return {name: np.full(shape, 2, np.float16) for name, shape in SPEC.output_shapes.items()}
  engine = NS(inputs={n: NS(shape=s) for n, s in SPEC.input_shapes.items()},
              outputs={n: NS(shape=s) for n, s in SPEC.output_shapes.items()},
              host_input=dest.__getitem__, configure_state_feedback=lambda _: None,
              warm=lambda: 'warm', run=run, last_gpu_us=1)
  loaded = EngineHost._warm(None, engine, SPEC)
  assert loaded.queues._fast_inputs is not None
  replies = []
  transport = NS(protocol_version=2, send=lambda kind, seq, parts, flags:
                 replies.append(b''.join(memoryview(part).cast('B') for part in parts)))
  session = Session(transport, NS(telemetry=NS(read=dict), frame_stats=NS(record=lambda *a: None)))
  image = np.arange(SPEC.warped_nbytes, dtype=np.uint8).reshape(SPEC.warped_shape)
  packed = np.zeros(SPEC.packed_nelem, np.float32)
  packed[12:12 + 65536] = np.arange(65536, dtype=np.uint8)
  compact, _ = encode(packed, SPEC)
  payload = image.tobytes() + compact
  flags = FLAG | P.Flag.RESET_QUEUES
  if compressed:
    payload = block_codec.encode((payload,))
    assert payload is not None
    flags |= block_codec.FLAG
  request = P.pack_infer_req(7, flags) + payload
  session._infer(loaded, NS(seq=7, payload=memoryview(request)))
  assert P.unpack_infer_resp(memoryview(replies[-1]))[:2] == (7, P.Status.OK)
  for name in SPEC.state_pairs:
    assert not seen[0][name].any()
    assert (loaded.queues.state[name] == 2).all()
  expected = buffers(np.float16)
  make_queues(SPEC, expected).step_into(image, packed, expected)
  for name in expected:
    assert seen[0][name].tobytes() == expected[name].tobytes()
  # Invalid reset-marked frames cannot reset accepted state or enter the engine.
  session._infer(loaded, NS(seq=8, payload=memoryview(request[:-1])))
  assert P.unpack_infer_resp(memoryview(replies[-1]))[1] == P.Status.BAD_SHAPE
  assert len(seen) == 1
  assert all((value == 2).all() for value in loaded.queues.state.values())
