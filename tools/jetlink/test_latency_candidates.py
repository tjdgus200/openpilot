"""CPU regression tests. Fake CUDA checks ordering/lifetime, not GPU numerics."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / 'third_party/jetlink'
sys.path.insert(0, str(VENDOR))
from jetlink.finite import FiniteChecker
from jetlink.queues import StatefulQueues
from jetlink.spec import ModelSpec
from jetlink.transport.base import StreamTransport, take
from jetlink.transport.ffs import FfsTransport
from jetlink import protocol as P


SPEC = ModelSpec('a' * 64, 100, 4,
  {'new_img': (2, 6, 2, 2), 'desire': (8,), 'traffic_convention': (1, 2),
   'action_t': (1, 2), 'state_a': (3, 8), 'state_b': (2, 3)},
  {'outputs': (1, 6), 'next_state_a': (3, 8), 'next_state_b': (2, 3)}, {}, None)


class TestFinite(unittest.TestCase):
  def test_all_65536_half_patterns(self):
    values = np.arange(65536, dtype=np.uint16).view(np.float16)
    checker = FiniteChecker(values.size)
    self.assertFalse(checker(values))
    np.testing.assert_array_equal(checker.valid, np.isfinite(values))
    finite = values[np.isfinite(values)]
    self.assertTrue(checker(finite))
    for bit in (0x7c00, 0xfc00, 0x7c01, 0x7fff, 0xfc01, 0xffff):
      self.assertFalse(checker(np.array([bit], np.uint16).view(np.float16)))

  def test_other_dtypes_layout_and_capacity(self):
    checker = FiniteChecker(10)
    for dtype in (np.float16, np.float32, np.float64, np.int32, '>f2'):
      for count in (0, 1, 10, 11):
        values = np.arange(count, dtype=dtype)
        self.assertEqual(checker(values), bool(np.isfinite(values).all()))
    a = np.arange(16, dtype=np.float16).reshape(4, 4).T
    self.assertTrue(checker(a))
    a[1, 1] = np.nan
    self.assertFalse(checker(a))


class FakeCuda:
  def __init__(self):
    self.allocations = {}
    self.host_freed = []
    self.device_freed = []
    self.capture = None
    self.pending = []

  def alloc(self, size):
    a = (ctypes.c_char * size)()
    ptr = ctypes.addressof(a)
    self.allocations[ptr] = a
    return ptr

  malloc = host_alloc = alloc

  def free(self, ptr):
    assert ptr not in self.device_freed
    self.device_freed.append(ptr)
    del self.allocations[ptr]

  def host_free(self, ptr):
    assert ptr not in self.host_freed
    self.host_freed.append(ptr)
    del self.allocations[ptr]

  def enqueue(self, fn):
    (self.capture if self.capture is not None else self.pending).append(fn)

  def copy(self, dst, src, size, stream):
    self.enqueue(lambda: ctypes.memmove(dst, src, size))

  memcpy_h2d_async = memcpy_d2h_async = copy
  def stream_create(self): return 1
  def stream_destroy(self, stream): pass
  def stream_begin_capture(self, stream): self.capture = []
  def stream_end_capture(self, stream):
    graph, self.capture = self.capture, None
    return graph
  def graph_instantiate(self, graph): return list(graph)
  def graph_destroy(self, graph): pass
  def graph_exec_destroy(self, graph): pass
  def graph_launch(self, graph, stream): self.pending.extend(graph)
  def stream_sync(self, stream):
    while self.pending:
      self.pending.pop(0)()


def load_fake_engine_module(cuda):
  modes = types.SimpleNamespace(INPUT=1, OUTPUT=2)
  typeset = types.SimpleNamespace(**{n: i for i, n in enumerate(
    ('FLOAT', 'HALF', 'INT8', 'INT32', 'INT64', 'BOOL', 'UINT8'))})
  shapes = SPEC.input_shapes | SPEC.output_shapes
  dtypes = {n: (np.float16 if n not in ('outputs',) else np.float32) for n in shapes}

  class Context:
    def __init__(self): self.addresses = {}
    def set_tensor_address(self, name, ptr): self.addresses[name] = ptr
    def array(self, name):
      size = int(np.prod(shapes[name])) * np.dtype(dtypes[name]).itemsize
      return np.frombuffer((ctypes.c_char * size).from_address(self.addresses[name]), dtypes[name]).reshape(shapes[name])
    def execute_async_v3(self, stream):
      def compute():
        # Copy reads first: emulate a deterministic recurrent graph, not TRT.
        values = {n: self.array(n).copy() for n in SPEC.input_shapes}
        increment = float(values['new_img'].flat[0]) + 1
        for name, output in SPEC.state_pairs.items():
          self.array(output)[:] = values[name] * np.float16(.5) + np.float16(increment)
        self.array('outputs')[:] = float(values['state_a'].sum()) + increment
      cuda.enqueue(compute)
      return True

  class Engine:
    num_io_tensors = len(shapes)
    def get_tensor_name(self, i): return list(shapes)[i]
    def get_tensor_mode(self, name): return modes.INPUT if name in SPEC.input_shapes else modes.OUTPUT
    def get_tensor_shape(self, name): return shapes[name]
    def get_tensor_dtype(self, name): return typeset.FLOAT if dtypes[name] == np.float32 else typeset.HALF
    def create_execution_context(self): return Context()

  class Logger:
    WARNING = 2
    def __init__(self, level): pass
  class Runtime:
    def __init__(self, logger): pass
    def deserialize_cuda_engine(self, data): return Engine()

  trt = types.ModuleType('tensorrt')
  trt.Logger, trt.Runtime, trt.DataType, trt.TensorIOMode = Logger, Runtime, typeset, modes
  package = types.ModuleType('jetlink.server.backends.trt')
  package.cudart = cuda
  spec = importlib.util.spec_from_file_location('test_trt_engine', VENDOR/'jetlink/server/backends/trt/engine.py')
  module = importlib.util.module_from_spec(spec)
  with patch.dict(sys.modules, {'tensorrt': trt, 'jetlink.server.backends.trt': package, 'test_trt_engine': module}):
    spec.loader.exec_module(module)
  return module


class TestStateReuse(unittest.TestCase):
  def new(self, share):
    cuda = FakeCuda()
    module = load_fake_engine_module(cuda)
    with tempfile.NamedTemporaryFile(delete=False) as f:
      f.write(b'fake plan for buffer semantics test')
      plan = f.name
    try:
      engine = module.TrtEngine(plan)
    finally:
      Path(plan).unlink()
    if share:
      engine.share_state_host_buffers(SPEC.state_pairs)
    inputs = {n: engine.host_input(n) for n in engine.inputs}
    queues = StatefulQueues(SPEC, inputs)
    queues.step_into(np.zeros(SPEC.warped_shape, np.uint8), np.zeros(SPEC.packed_nelem, np.float32), inputs)
    return engine, queues, inputs, cuda

  def test_reference_equivalence_capture_reset_and_allocation_lifetime(self):
    for graph in (False, True):
      for fast in ('0', '1'):
        with self.subTest(graph=graph, fast=fast), patch.dict(os.environ, JETLINK_FAST_HALF_CHECK=fast):
          reference = self.new(False)
          optimized = self.new(True)
          for engine, queues, inputs, cuda in (reference, optimized):
            if graph:
              engine.warm()
            else:
              engine.run()
            queues.reset()
          for frame in range(20):
            outputs = []
            for engine, queues, inputs, cuda in (reference, optimized):
              if frame == 7:
                queues.reset()
              queues.step_into(np.full(SPEC.warped_shape, frame % 4, np.uint8), np.zeros(SPEC.packed_nelem, np.float32), inputs)
              out = engine.run()
              queues.feedback(out)
              outputs.append({n: x.copy() for n, x in out.items()})
            for name in outputs[0]:
              np.testing.assert_array_equal(outputs[0][name], outputs[1][name])
          engine, queues, inputs, cuda = optimized
          with self.assertRaises(RuntimeError):
            engine.share_state_host_buffers(SPEC.state_pairs)
          for engine, queues, inputs, cuda in (reference, optimized):
            engine.close()
            self.assertEqual(cuda.allocations, {})
            engine.close()  # no duplicate buffer release

  def test_nonfinite_in_each_output_resets_every_state(self):
    for share in (False, True):
      for fast in ('0', '1'):
        for bad in SPEC.output_shapes:
          with self.subTest(share=share, fast=fast, bad=bad), patch.dict(os.environ, JETLINK_FAST_HALF_CHECK=fast):
            engine, queues, inputs, cuda = self.new(share)
            out = engine.run()
            out[bad].flat[-1] = np.nan
            with self.assertRaises(FloatingPointError): queues.feedback(out)
            self.assertTrue(all(np.count_nonzero(v) == 0 for v in queues.state.values()))
            queues.step_into(np.zeros(SPEC.warped_shape, np.uint8), np.zeros(SPEC.packed_nelem, np.float32), inputs)
            out = engine.run()
            queues.feedback(out)
            self.assertTrue(all(np.isfinite(x).all() for x in out.values()))
            engine.close()

  def test_shape_dtype_and_late_pair_errors_fail_closed(self):
    for error in ('missing', 'shape', 'dtype'):
      engine, queues, inputs, cuda = self.new(True)
      out = engine.run()
      if error == 'missing': del out['next_state_b']
      if error == 'shape': out['next_state_b'] = out['next_state_b'].reshape(-1)
      if error == 'dtype': out['next_state_b'] = out['next_state_b'].astype(np.float32)
      with self.assertRaises(ValueError): queues.feedback(out)
      self.assertTrue(all(np.count_nonzero(v) == 0 for v in queues.state.values()))
      engine.close()
    engine, queues, inputs, cuda = self.new(False)
    before = engine.inputs['state_a'].host
    with self.assertRaises(ValueError):
      engine.share_state_host_buffers({'state_a': 'next_state_a', 'state_b': 'missing'})
    self.assertIs(engine.inputs['state_a'].host, before)
    with self.assertRaises(ValueError):
      engine.share_state_host_buffers({'state_a': 'next_state_a', 'state_b': 'next_state_a'})
    engine.close()

  def test_failed_shared_transfer_requires_reload(self):
    engine, queues, inputs, cuda = self.new(True)
    with patch.object(cuda, 'stream_sync', side_effect=RuntimeError('injected CUDA failure')):
      with self.assertRaisesRegex(RuntimeError, 'injected CUDA failure'): engine.run()
    queues.reset()
    with self.assertRaisesRegex(RuntimeError, 'reload the engine'): engine.run()
    cuda.pending.clear()
    engine.close()

  def test_session_warm_wires_sharing_before_views_and_resets(self):
    from jetlink.server.session import EngineHost
    from types import SimpleNamespace
    cuda=FakeCuda()
    module=load_fake_engine_module(cuda)
    with tempfile.NamedTemporaryFile(delete=False) as f:
      f.write(b'fake plan')
      plan=f.name
    try: engine=module.TrtEngine(plan)
    finally:Path(plan).unlink()
    host=EngineHost(SimpleNamespace())
    try:
      with patch.dict(os.environ, JETLINK_SHARE_STATE_HOST='1', JETLINK_FAST_HALF_CHECK='1'):
        loaded=host._warm(engine,SPEC)
      for name, output in SPEC.state_pairs.items():
        self.assertIs(loaded.host_inputs[name],engine.outputs[output].host)
        self.assertEqual(np.count_nonzero(loaded.host_inputs[name]),0)
      out=engine.run()
      loaded.queues.feedback(out)
      self.assertTrue(all(np.isfinite(v).all() for v in out.values()))
    finally:
      engine.close()
      host.close()


class CaptureFfs(FfsTransport):
  def __init__(self, fail_above=None):
    StreamTransport.__init__(self)
    self.writes, self.bytes, self.shrinks = [], bytearray(), 0
    self.fail_above = fail_above
  def _write(self, bufs):
    size = sum(x.nbytes for x in bufs)
    if self.fail_above and size > self.fail_above:
      if not self._shrink_write(): raise RuntimeError('simulated ENOMEM floor')
      self.shrinks += 1
      return self._write(take(bufs, self.write_chunk))
    self.writes.append(size)
    self.bytes.extend(b''.join(bufs))
    return size
  def _read_into(self, dest, timeout): raise NotImplementedError
  def close(self): pass


class TestUsb(unittest.TestCase):
  def test_real_nav_payload_byte_identity_and_write_count(self):
    parts = [P.pack_infer_req(17, 0), bytes(393216), bytes(262380)]
    captures = []
    for enable in ('0', '1'):
      with patch.dict(os.environ, JETLINK_COALESCE_INFER_WRITE=enable):
        transport = CaptureFfs()
        transport.send(P.Msg.INFER_REQ, 9, parts)
        captures.append(transport)
    self.assertEqual(captures[0].bytes, captures[1].bytes)
    self.assertEqual(len(captures[0].writes), 2)
    self.assertEqual(len(captures[1].writes), 1)

  def test_memory_pressure_fallback_and_next_message_retry(self):
    parts = [P.pack_infer_req(17, 0), bytes(393216), bytes(262380)]
    with patch.dict(os.environ, JETLINK_COALESCE_INFER_WRITE='1'):
      constrained = CaptureFfs(fail_above=512*1024)
      constrained.send(P.Msg.INFER_REQ, 9, parts)
      healthy = CaptureFfs()
      healthy.send(P.Msg.INFER_REQ, 9, parts)
      self.assertEqual(constrained.bytes, healthy.bytes)
      self.assertEqual(constrained.shrinks, 1)
      constrained.fail_above = None
      constrained.writes.clear()
      constrained.send(P.Msg.INFER_REQ, 10, parts)
      self.assertEqual(len(constrained.writes), 1)

  def test_non_inference_and_disabled_default_unchanged(self):
    for kind in (P.Msg.HELLO_REQ, P.Msg.INFER_RESP):
      values = []
      for enable in ('0', '1'):
        with patch.dict(os.environ, JETLINK_COALESCE_INFER_WRITE=enable):
          transport = CaptureFfs()
          transport.send(kind, 4, (bytes(700000),))
          values.append((transport.writes, transport.bytes))
      self.assertEqual(values[0], values[1])


if __name__ == '__main__':
  unittest.main(verbosity=2)
