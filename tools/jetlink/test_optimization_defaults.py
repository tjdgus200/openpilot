"""PV5 merge gate: optimized defaults, exact model gates and safe negotiation.

No device or performance claim: CUDA is deferred fake work and IPC is mocked.
Explicit diagnostic opt-outs and capability fallbacks remain available.
"""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from jetlink import deferred_map, output_projection as projection
from jetlink.server.session import EngineHost
from jetlink.spec import ModelSpec
from openpilot.selfdrive.modeld.jetlink import link, model
from test_device_state import runtime  # noqa: F401 -- deferred CUDA fixture
from test_hotplug_resources import joining_model

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=['n16', 'md-v3', 'ctv3m', 'light-speed'])
def spec(request):
  path = ROOT / f'openpilot/selfdrive/modeld/model_catalog/{request.param}.json'
  return ModelSpec.from_dict(json.loads(path.read_text(encoding='utf-8')))


def bindings(runtime, spec):
  e = runtime.engine.TrtEngine.__new__(runtime.engine.TrtEngine)
  e.stream, e.graph_exec, e.device_state, e._state_host_sources = 1, None, None, {}
  e.bindings = {}
  for is_input, shapes in ((True, spec.input_shapes), (False, spec.output_shapes)):
    for name, shape in shapes.items():
      dtype = np.dtype(np.float16 if name in ('new_img', 'state_img_q', 'next_state_img_q') else np.float32)
      size = int(np.prod(shape)) * dtype.itemsize
      device, host = runtime.cuda.malloc(size), runtime.cuda.host_alloc(size)
      array = runtime.engine._np_from_ptr(host, shape, dtype)
      array.fill(0)
      e.bindings[name] = runtime.engine.Binding(name, shape, dtype, size, device, host, array, is_input)
  e.inputs = {n: b for n, b in e.bindings.items() if b.is_input}
  e.outputs = {n: b for n, b in e.bindings.items() if not b.is_input}
  # All allocated fake-device outputs start at zero. Only routing is tested here;
  # test_device_state separately exercises recurrence, faults and reset order.
  e.context = NS(execute_async_v3=lambda stream: True)
  return e


def test_default_gpu_state_graph_image_and_n16_staging(runtime, monkeypatch, spec):
  from jetlink import input_cast
  for name in ('CARROT_TRT_DEVICE_STATE', 'CARROT_TRT_NATIVE_IMAGE', 'CARROT_TRT_DIRECT_MAP'):
    monkeypatch.delenv(name, raising=False)
  native_requests = []
  monkeypatch.setattr(input_cast, 'native_u8_to_f16', lambda: native_requests.append(True))
  engine = bindings(runtime, spec)
  try:
    loaded = EngineHost._warm(None, engine, spec)
    assert engine.device_state is not None and engine.graph_exec is not None
    assert engine.device_state.input_names == set(spec.state_pairs)
    n16 = spec.sha256 == '01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a'
    assert projection.eligible(spec) == n16
    assert bool(native_requests) == n16
    assert (loaded.queues.direct_map is not None) == n16
    pixels = np.arange(spec.warped_nbytes, dtype=np.uint8).reshape(spec.warped_shape)
    loaded.queues.step_into(pixels, np.zeros(spec.packed_nelem, np.float32), loaded.host_inputs)
    np.testing.assert_array_equal(loaded.host_inputs['new_img'], pixels.astype(np.float16))
  finally:
    engine.close()
  assert not runtime.cuda.pending and not runtime.cuda.memory


@pytest.mark.parametrize('capable,supported', [(True, True), (False, True), (True, False)])
def test_default_shared_ram_and_compact_reply_negotiation(monkeypatch, spec, capable, supported):
  for name in ('JETLINK_RAM_INPUT', 'JETLINK_COMPACT_REPLY', 'JETLINK_DEFER_NAV', 'SEND_RAW_PRED'):
    monkeypatch.delenv(name, raising=False)
  hello = {'spec': spec.to_dict(), link.shared_input.CAPABILITY: capable,
           projection.IPC_CAPABILITY: projection.descriptor(),
           deferred_map.IPC_CAPABILITY: deferred_map.descriptor()}
  sock = NS(settimeout=lambda *a: None, connect=lambda *a: None, close=lambda: None)
  shared = NS(close=lambda: None)
  sent = []
  monkeypatch.setattr(link, 'socket', NS(socket=lambda *a: sock, AF_UNIX=object(), SOCK_STREAM=object()))
  monkeypatch.setattr(link, 'receive', lambda *a: json.dumps(hello).encode())
  monkeypatch.setattr(link, 'send_parts', lambda sock, *parts, **kw: sent.append(parts))
  monkeypatch.setattr(link.shared_input, 'supported', lambda: supported)
  monkeypatch.setattr(link.shared_input.Writer, 'receive', lambda *a: shared)
  client = link.Client('mock-only')
  try:
    assert (client.shared is shared) == (capable and supported)
    assert sent == ([(link.shared_input.SETUP,)] if capable and supported else [])
    assert client.project_output == projection.eligible(spec)
    assert client.deferred_nav == projection.eligible(spec)
  finally:
    client.close()


def test_cpu_preparation_overlap_is_default_for_every_model(monkeypatch, spec):
  monkeypatch.delenv('JETLINK_CPU_OVERLAP', raising=False)
  m = joining_model(monkeypatch)
  m.spec = spec
  m.packed = np.zeros(spec.packed_nelem, np.float32)
  m.views = {}
  offset = 0
  for name, shape in spec.packed_shapes.items():
    size = int(np.prod(shape))
    m.views[name] = m.packed[offset:offset + size].reshape(shape)
    offset += size
  calls = []
  def warp(bufs, transforms, *, prepare=None, output_buffer=None):
    calls.append('submit')
    if prepare is not None:
      prepare()
    calls.append('read')
    return np.arange(16, dtype=np.uint8)
  m.warp = warp
  original = model.fill_inputs
  def fill(*args):
    calls.append('prepare')
    original(*args)
  monkeypatch.setattr(model, 'fill_inputs', fill)
  def infer(image, packed, frame, reset, source_sof):
    calls.append('usb')
    assert calls == ['submit', 'prepare', 'read', 'usb']
    assert image.tobytes() == np.arange(16, dtype=np.uint8).tobytes()
    assert packed.size == spec.packed_nelem and reset
    return np.zeros(spec.output_nelem, np.float32)
  m.client = NS(image_buffer=lambda: None, infer=infer, timings=(0, 0, 0),
                close=lambda: pytest.fail('unexpected local fallback'))
  inputs = {'desire_pulse': np.eye(8, dtype=np.float32)[2],
            'traffic_convention': np.array([1, 0], np.float32), 'action_t': np.array([.2, .3], np.float32),
            'nav_map': np.arange(65536, dtype=np.uint8).reshape(256, 256),
            'navigation': np.arange(47, dtype=np.float32)}
  assert m.run({}, {}, inputs, False) == 'external'
