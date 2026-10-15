"""Optional real CUDA/NVRTC/graph check, independent of TensorRT/model files.

Run with CARROT_TEST_CUDA=1 and cuda-python + CUDA runtime/NVRTC installed.
Passing on a desktop GPU does not validate the Orin TensorRT execution path.
"""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.getenv('CARROT_TEST_CUDA') != '1', reason='explicit native CUDA test')
ROOT = Path(__file__).resolve().parents[2]


def load(monkeypatch, name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  mod = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, name, mod)
  spec.loader.exec_module(mod)
  return mod


def test_actual_gpu_finite_scan_graph_event_recurrence_and_reset(monkeypatch):
  folder = ROOT / 'third_party/jetlink/jetlink/server/backends/trt'
  package = ModuleType('jetlink.server.backends.trt')
  package.__path__ = [str(folder)]
  monkeypatch.setitem(sys.modules, package.__name__, package)
  cuda = load(monkeypatch, package.__name__ + '.cudart', folder / 'cudart.py')
  package.cudart = cuda
  state_module = load(monkeypatch, package.__name__ + '.device_state', folder / 'device_state.py')
  cuda.set_device(0)
  stream = cuda.stream_create()
  ins, outs, allocations = {}, {}, []
  graph = executable = state = None
  try:
    for key, dtype in [('half', np.float16), ('float', np.float32)]:
      for mapping, name in [(ins, key), (outs, 'next_' + key)]:
        host = np.zeros(32, dtype)
        pointer = cuda.malloc(host.nbytes)
        allocations.append(pointer)
        mapping[name] = NS(name=name, shape=host.shape, dtype=host.dtype, nbytes=host.nbytes,
                           device_ptr=int(pointer), host=host)
    state = state_module.DeviceState(ins, outs, {'half': 'next_half', 'float': 'next_float'}, stream)
    # Pay for module loading/kernel launch before capturing.
    for b in outs.values():
      cuda.memcpy_h2d_async(b.device_ptr, b.host.ctypes.data, b.nbytes, stream)
    state.enqueue_check()
    cuda.stream_sync(stream)
    state.validate()
    cuda.stream_begin_capture(stream)
    state.enqueue_check()
    cuda.event_record(state.reply, stream)
    state.enqueue_feedback()
    graph = cuda.stream_end_capture(stream)
    executable = cuda.graph_instantiate(graph)
    # Exponent boundaries, signed zeros, subnormals, infinities and NaN payloads.
    cases = [('half', b, (b & 0x7c00) == 0x7c00) for b in
             [0, 0x8000, 1, 0x03ff, 0x0400, 0x7bff, 0xfbff, 0x7c00, 0xfc00, 0x7c01, 0x7fff, 0xfc01]]
    cases += [('float', b, (b & 0x7f800000) == 0x7f800000) for b in
              [0, 0x80000000, 1, 0x007fffff, 0x00800000, 0x7f7fffff, 0xff7fffff,
               0x7f800000, 0xff800000, 0x7f800001, 0x7fffffff, 0xff800001]]
    for key, bits, bad in cases:
      state.reset()
      state.before_run()
      # Reset must follow any previous trailing feedback on the same stream.
      for b in ins.values():
        cuda.memcpy_d2h_async(b.host.ctypes.data, b.device_ptr, b.nbytes, stream)
      cuda.stream_sync(stream)
      assert all(not np.any(b.host.view(np.uint8)) for b in ins.values())
      for b in outs.values():
        b.host.fill(1)
      value = outs['next_' + key].host
      value.view(np.uint16 if key == 'half' else np.uint32)[-1] = bits
      for b in outs.values():
        cuda.memcpy_h2d_async(b.device_ptr, b.host.ctypes.data, b.nbytes, stream)
      cuda.graph_launch(executable, stream)
      cuda.event_sync(state.reply)
      if bad:
        with pytest.raises(FloatingPointError):
          state.validate()
      else:
        state.validate()
      for b in ins.values():
        cuda.memcpy_d2h_async(b.host.ctypes.data, b.device_ptr, b.nbytes, stream)
      cuda.stream_sync(stream)
      for k, b in ins.items():
        np.testing.assert_array_equal(b.host.view(np.uint8), outs['next_' + k].host.view(np.uint8))
  finally:
    cuda.stream_sync(stream)
    if executable is not None:
      cuda.graph_exec_destroy(executable)
    if graph is not None:
      cuda.graph_destroy(graph)
    if state is not None:
      state.close()
    for pointer in allocations:
      cuda.free(pointer)
    cuda.stream_destroy(stream)
