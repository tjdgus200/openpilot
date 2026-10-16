"""Execute the real engine/queue logic against deferred CUDA operations.

This checks ordering, recurrence and error/reset behavior, not CUDA ABI or Orin
timing. Native finite-kernel, graph and end-to-end tests remain mandatory.
"""
import ctypes
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'third_party/jetlink'))
from jetlink.queues import make_queues


def load(monkeypatch, name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, name, module)
  spec.loader.exec_module(module)
  return module


class Cuda:
  def __init__(self):
    self.arrays, self.todo, self.ops = [], [], []
    self.capture = None

  def alloc(self, n):
    array = np.zeros(n, np.uint8)
    self.arrays.append(array)
    return array.ctypes.data

  malloc = host_alloc = alloc

  def add(self, tag, action):
    self.ops.append(tag)
    (self.todo if self.capture is None else self.capture).append((tag, action))

  @staticmethod
  def bytes(ptr, n):
    return np.ctypeslib.as_array((ctypes.c_ubyte * n).from_address(int(ptr)))

  def copy(self, kind, dest, source, n, stream):
    self.add((kind, n), lambda: np.copyto(self.bytes(dest, n), self.bytes(source, n)))

  def memcpy_h2d_async(self, *a): self.copy('h2d', *a)
  def memcpy_d2h_async(self, *a): self.copy('d2h', *a)
  def memcpy_d2d_async(self, *a): self.copy('d2d', *a)
  def memset_async(self, ptr, n, stream): self.add(('zero', n), lambda: self.bytes(ptr, n).fill(0))
  def event_create(self): return 11
  def event_record(self, event, stream, *, external=False):
    assert external == (self.capture is not None), 'external event must be limited to graph capture'
    self.add(('event', event), lambda: None)

  def event_sync(self, event):
    while self.todo:
      tag, action = self.todo.pop(0)
      action()
      if tag == ('event', event):
        return
    raise AssertionError('event was not recorded')

  def stream_sync(self, stream):
    while self.todo:
      self.todo.pop(0)[1]()

  def stream_begin_capture(self, stream):
    assert self.capture is None
    self.capture = []

  def stream_end_capture(self, stream):
    graph, self.capture = self.capture, None
    return graph

  def graph_instantiate(self, graph): return graph
  def graph_launch(self, graph, stream): self.todo.extend(graph)
  def graph_destroy(self, graph): pass
  def graph_exec_destroy(self, graph): pass
  def free(self, ptr): assert not self.todo
  def host_free(self, ptr): assert not self.todo
  def event_destroy(self, event): assert not self.todo
  def stream_destroy(self, stream): assert not self.todo


@pytest.fixture
def runtime(monkeypatch):
  cuda = Cuda()
  package = ModuleType('jetlink.server.backends.trt')
  package.__path__ = [str(ROOT / 'third_party/jetlink/jetlink/server/backends/trt')]
  package.cudart = cuda
  monkeypatch.setitem(sys.modules, package.__name__, package)
  monkeypatch.setitem(sys.modules, 'tensorrt', NS(Logger=NS(WARNING=1), DataType=NS(
    **{name: i for i, name in enumerate(('FLOAT', 'HALF', 'INT8', 'INT32', 'INT64', 'BOOL', 'UINT8'))})))
  folder = Path(package.__path__[0])
  state = load(monkeypatch, package.__name__ + '.device_state', folder / 'device_state.py')
  engine_module = load(monkeypatch, package.__name__ + '.engine', folder / 'engine.py')

  class Kernel:
    def launch(self, binding, bad, stream):
      def check():
        value = cuda.bytes(binding.device_ptr, binding.nbytes).view(binding.dtype)
        if not np.isfinite(value).all():
          cuda.bytes(bad, 4).view(np.uint32)[0] |= 1
      cuda.add(('finite', binding.name), check)
    def close(self): assert not cuda.todo

  original = state.DeviceState
  monkeypatch.setattr(state, 'DeviceState', lambda *a: original(*a, kernel_factory=Kernel))
  return cuda, engine_module, original, Kernel


SPEC = NS(stateful=True, warped_shape=(2, 6, 2, 2), packed_shapes={'desire': (1, 1)},
          packed_nelem=1, packed_sizes=(1,),
          state_pairs={'state_img': 'next_img', 'state_feat': 'next_feat'},
          input_shapes={'new_img': (2, 6, 2, 2), 'desire': (1, 1), 'state_img': (1, 4), 'state_feat': (1, 2)},
          output_shapes={'outputs': (1, 3), 'next_img': (1, 4), 'next_feat': (1, 2)})


def build(runtime, monkeypatch, mode):
  cuda, module, _, _ = runtime
  engine = module.TrtEngine.__new__(module.TrtEngine)
  engine.stream, engine.graph_exec, engine.device_state = 1, None, None
  engine._state_host_sources = {}
  engine.bindings = {}
  for inp, shapes in ((True, SPEC.input_shapes), (False, SPEC.output_shapes)):
    for name, shape in shapes.items():
      dtype = np.dtype('float16' if name in ('new_img', 'state_img', 'next_img') else 'float32')
      n = int(np.prod(shape)) * dtype.itemsize
      device, host = cuda.alloc(n), cuda.alloc(n)
      engine.bindings[name] = module.Binding(name, shape, dtype, n, device, host,
                                             module._np_from_ptr(host, shape, dtype), inp)
  engine.inputs = {n: b for n, b in engine.bindings.items() if b.is_input}
  engine.outputs = {n: b for n, b in engine.bindings.items() if not b.is_input}
  engine.inject = None
  def execute(stream):
    def compute():
      def arr(name):
        b = engine.bindings[name]
        return cuda.bytes(b.device_ptr, b.nbytes).view(b.dtype)
      for a, b in SPEC.state_pairs.items():
        arr(b)[:] = arr(a) + arr('new_img')[0] + 1
      arr('outputs')[:] = sum(arr(n).sum() for n in SPEC.state_pairs)
      if engine.inject is not None:
        arr(engine.inject)[0] = np.nan
    cuda.add(('infer',), compute)
    return True
  engine.context = NS(execute_async_v3=execute)
  monkeypatch.setenv('CARROT_TRT_DEVICE_STATE', '1' if mode else '0')
  engine.configure_state_feedback(SPEC.state_pairs)
  inputs = {n: engine.host_input(n) for n in engine.inputs}
  queues = make_queues(SPEC, inputs, engine.device_state)
  engine.warm()
  queues.reset()
  return engine, queues, inputs


def test_gpu_recurrence_matches_host_feedback_across_frames_resets_and_graph(runtime, monkeypatch):
  cuda = runtime[0]
  results = []
  for mode in (False, True):
    engine, queues, inputs = build(runtime, monkeypatch, mode)
    outputs = []
    for i in range(40):
      if i in (7, 19):
        queues.reset()
      queues.step_into(np.full(SPEC.warped_shape, i % 4, np.uint8), np.zeros(1, np.float32), inputs)
      result = engine.run()
      queues.feedback(result)
      outputs.append(result['outputs'].copy())
      if mode:
        assert cuda.todo and all(tag[0] == 'd2d' for tag, _ in cuda.todo)
    engine.close()  # synchronizes outstanding state copies
    results.append(outputs)
  np.testing.assert_array_equal(results[0], results[1])


@pytest.mark.parametrize('bad', ['next_img', 'next_feat', 'outputs'])
def test_any_nonfinite_output_rejected_and_zero_state_restored(runtime, monkeypatch, bad):
  engine, queues, inputs = build(runtime, monkeypatch, True)
  engine.inject = bad
  queues.step_into(np.zeros(SPEC.warped_shape, np.uint8), np.zeros(1, np.float32), inputs)
  result = engine.run()
  with pytest.raises(FloatingPointError):
    queues.feedback(result)
  engine.inject = None
  result = engine.run()
  queues.feedback(result)
  np.testing.assert_array_equal(result['outputs'], 0)
  engine.close()


def test_scalar_failure_resets_gpu_history(runtime, monkeypatch):
  engine, queues, inputs = build(runtime, monkeypatch, True)
  queues.feedback(engine.run())
  with pytest.raises(ValueError, match='Non-finite'):
    queues.step_into(np.zeros(SPEC.warped_shape, np.uint8), np.array([np.inf], np.float32), inputs)
  result = engine.run()
  queues.feedback(result)
  np.testing.assert_array_equal(result['outputs'], 0)
  engine.close()


def test_device_path_excludes_state_host_copies_but_checks_before_reply(runtime, monkeypatch):
  cuda = runtime[0]
  engine, _, _ = build(runtime, monkeypatch, True)
  cuda.stream_sync(1)
  cuda.ops.clear()
  engine.graph_exec = None
  engine.run()
  tags = [op[0] for op in cuda.ops]
  assert tags.index('infer') < tags.index('finite') < tags.index('event') < tags.index('d2d')
  assert sum(op[1] for op in cuda.ops if op[0] == 'd2h') == 3 * 4 + 4
  assert sum(op[1] for op in cuda.ops if op[0] == 'h2d') == 48 * 2 + 4
  engine.close()


def test_opt_in_initialization_failure_is_not_silently_ignored(runtime, monkeypatch):
  engine, _, _ = build(runtime, monkeypatch, False)
  monkeypatch.setenv('CARROT_TRT_DEVICE_STATE', '1')
  module = sys.modules['jetlink.server.backends.trt.device_state']
  def fail(*a): raise RuntimeError('NVRTC unavailable')
  monkeypatch.setattr(module, 'DeviceState', fail)
  engine.graph_exec = None
  with pytest.raises(RuntimeError, match='NVRTC'):
    engine.configure_state_feedback(SPEC.state_pairs)
  engine.close()


def test_explicit_state_upload_cannot_be_silently_ignored(runtime, monkeypatch):
  engine, _, inputs = build(runtime, monkeypatch, True)
  try:
    with pytest.raises(RuntimeError, match='queue-managed'):
      engine.infer(inputs)
  finally:
    engine.close()


@pytest.mark.parametrize('dtype', ['int32', 'float64'])
def test_unknown_state_dtype_refused_before_allocating_gpu_resources(runtime, dtype):
  _, _, state, _ = runtime
  b = NS(shape=(1,), dtype=np.dtype(dtype), nbytes=4)
  with pytest.raises(ValueError, match='contract'):
    state({'s': b}, {'next': b}, {'s': 'next'}, 1)
