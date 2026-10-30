"""Mock QCOM allocation, first-link lifetime and restore; never open a device."""
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.selfdrive.modeld.jetlink import coherent, model

SHAPE = (2, 6, 128, 256)


class Memory:
  def __init__(self, flags=0):
    self.meta = (NS(flags=flags),)
    self.data = np.zeros(SHAPE, np.uint8)


class Output:
  device = 'QCOM'
  nbytes = int(np.prod(SHAPE))
  _base = None
  def __init__(self):
    self._buf = Memory()
    self.retired = []
  def deallocate(self):
    self.retired.append(self._buf)
    self._buf = None
  def allocate(self, opaque):
    self._buf = opaque
  def is_allocated(self):
    return self._buf is not None


def capture():
  output = Output()
  return NS(captured=NS(ret=NS(shape=SHAPE, uop=NS(base=NS(buffer=output))),
                        _linear=object(), expected_names=[], expected_input_info=[]))


@pytest.fixture
def device(monkeypatch):
  calls = []
  dev = NS(flags=coherent.COHERENT_WRITEBACK, memory=[], freed=[], calls=calls)
  def allocate(size, flags):
    calls.append('allocate')
    assert size == Output.nbytes and flags == coherent.COHERENT_WRITEBACK
    memory = Memory(dev.flags)
    dev.memory.append(memory)
    return memory
  dev._gpu_alloc = allocate
  dev._gpu_free = dev.freed.append
  dev.synchronize = lambda: calls.append('fence')
  monkeypatch.setitem(sys.modules, 'tinygrad.device', NS(Device={'QCOM': dev}))
  return dev


def test_default_enabled_and_explicit_diagnostic_opt_out(monkeypatch):
  monkeypatch.delenv('JETLINK_COHERENT_WARP', raising=False)
  assert coherent.enabled()
  monkeypatch.setenv('JETLINK_COHERENT_WARP', '0')
  original = object()
  assert coherent.candidate(original, None, None, None) == (original, False)


@pytest.mark.parametrize('fault', ['linked', 'unknown_api', 'shape', 'size', 'device', 'view'])
def test_invalid_or_linked_buffer_is_never_retired(device, fault):
  jit = capture()
  result = jit.captured.ret
  if fault == 'linked':
    jit.captured.linear = object()
  if fault == 'unknown_api':
    del jit.captured._linear
  if fault == 'shape':
    result.shape = (16,)
  if fault == 'size':
    result.uop.base.buffer.nbytes = 16
  if fault == 'device':
    result.uop.base.buffer.device = 'AMD'
  if fault == 'view':
    result.uop.base.buffer._base = object()
  with pytest.raises(ValueError):
    coherent.install(jit, SHAPE)
  assert not device.calls and not result.uop.base.buffer.retired


@pytest.mark.parametrize('flags', [0, 1 << 31, 3 << 26])
def test_kernel_rejection_frees_candidate_and_preserves_original(device, flags):
  jit = capture()
  out = jit.captured.ret.uop.base.buffer
  original, device.flags = out._buf, flags
  with pytest.raises(ValueError, match='flags unsupported'):
    coherent.install(jit, SHAPE)
  assert out._buf is original and not out.retired
  assert device.freed == device.memory and device.calls == ['fence', 'allocate']


@pytest.mark.parametrize('stage', ['allocation', 'install'])
def test_allocation_or_install_failure_has_single_memory_owner(monkeypatch, device, stage):
  jit = capture()
  out = jit.captured.ret.uop.base.buffer
  def fail(*a, **kw): raise MemoryError('synthetic failure')
  if stage == 'allocation':
    monkeypatch.setattr(device, '_gpu_alloc', fail)
  else:
    monkeypatch.setattr(out, 'allocate', fail)
  with pytest.raises(MemoryError):
    coherent.install(jit, SHAPE)
  assert device.freed == device.memory
  assert out.is_allocated() == (stage == 'allocation')


def test_first_link_keeps_one_allocation_and_copyout_fence(device):
  jit = capture()
  out = jit.captured.ret.uop.base.buffer
  coherent.install(jit, SHAPE)
  captured_address = out._buf
  assert device.calls == ['fence', 'allocate'] and len(out.retired) == 1
  calls = []
  rng = np.random.default_rng(1300)
  target = np.empty(SHAPE, np.uint8)
  for _ in range(64):
    image = rng.integers(0, 256, SHAPE, dtype=np.uint8)
    def complete(image=image):
      calls.append('fence')
      captured_address.data[:] = image
      return captured_address.data.copy()
    def copyout(destination):
      destination[:] = memoryview(complete()).cast('B')
    result = NS(shape=SHAPE, numpy=complete, _buffer=lambda: NS(copyout=copyout))
    assert model.Warp.read_result(result, target) is target
    assert np.array_equal(target, image) and out._buf is captured_address
  assert calls == ['fence'] * 64 and len(device.memory) == 1 and not device.freed


@pytest.mark.parametrize('restore', ['valid', 'rejected', 'partial', 'disabled', 'old_package'])
def test_prepared_restore_installs_before_use_or_reloads_untouched_capture(monkeypatch, device, restore):
  from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
  first, clean = capture(), capture()
  identity = (1344, 760, get_nv12_info(1344, 760)[3], model.SPEC.sha256)
  loads = []
  def load(_):
    loads.append(True)
    return {'identity': identity, 'warp': first if len(loads) == 1 else clean,
            'coherent_output': restore != 'old_package'}
  monkeypatch.setitem(sys.modules, 'tinygrad', NS(Device={'QCOM': NS(device='QCOM')}))
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.helpers', NS(load_oob=load))
  monkeypatch.setitem(sys.modules, 'openpilot.common.swaglog', NS(cloudlog=NS(exception=lambda *a: None)))
  if restore == 'rejected':
    device.flags = 0
  if restore == 'partial':
    def fail(*a, **kw): raise MemoryError('synthetic install failure')
    first.captured.ret.uop.base.buffer.allocate = fail
  monkeypatch.setenv('JETLINK_COHERENT_WARP', '0' if restore == 'disabled' else '1')
  warp = model.Warp(1344, 760, prepared=b'mock', input_buffers=({}, {}))
  assert warp.coherent_output == (restore == 'valid')
  assert warp.run_warp is (clean if restore in ('rejected', 'partial') else first)
  assert len(loads) == (2 if restore in ('rejected', 'partial') else 1)
  assert len(device.memory) == (0 if restore in ('disabled', 'old_package') else 1)


@pytest.mark.parametrize('fault', [None, 'stale_pixels', 'read_error', 'drain_error', 'flags', 'serialization'])
def test_candidate_validates_distinct_frames_and_falls_back_cleanly(monkeypatch, device, fault):
  rng = np.random.default_rng(19)
  state = {'source': rng.integers(0, 256, SHAPE, dtype=np.uint8), 'calls': 0}
  class Capture:
    def __init__(self):
      self.captured = capture().captured
    def __call__(self, **kw):
      state['calls'] += 1
      if fault in ('read_error', 'drain_error'):
        if fault == 'drain_error':
          def fail(): raise RuntimeError('synthetic GPU drain failure')
          device.synchronize = fail
        raise RuntimeError('synthetic GPU read failure')
      data = np.zeros(SHAPE, np.uint8) if fault == 'stale_pixels' else state['source'].copy()
      return NS(numpy=lambda: data)
  original, proposed = Capture(), Capture()
  # Only the proposed capture gets the injected stale-read fault.
  original.__class__ = type('Reference', (Capture,), {'__call__': lambda self, **kw: NS(numpy=lambda: state['source'].copy())})
  def copyin(data):
    state['source'].reshape(-1)[:len(data)] = np.frombuffer(data, np.uint8)
  def tensor(*a, **kw):
    return NS(realize=lambda: NS(_buffer=lambda: NS(copyin=copyin)))
  monkeypatch.setitem(sys.modules, 'tinygrad', NS(Tensor=tensor))
  def dump(*a):
    if fault == 'serialization':
      raise ValueError('synthetic serialization failure')
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.helpers', NS(dump_oob=dump, load_oob=lambda _: proposed))
  monkeypatch.setitem(sys.modules, 'openpilot.common.swaglog', NS(cloudlog=NS(warning=lambda *a: None, exception=lambda *a: None)))
  if fault == 'flags':
    device.flags = 0
  transforms = {key: np.eye(3, dtype=np.float32) for key in ('tfm', 'big_tfm')}
  if fault == 'drain_error':
    with pytest.raises(RuntimeError, match='drain failure'):
      coherent.candidate(original, NS(size=32), transforms, {})
    return
  result, cached = coherent.candidate(original, NS(size=32), transforms, {})
  assert cached == (fault is None) and result is (proposed if cached else original)
  assert state['calls'] == (27 if fault is None else 1 if fault in ('stale_pixels', 'read_error') else 0)
  assert not original.captured.ret.uop.base.buffer.retired
  assert all(np.array_equal(matrix, np.eye(3)) for matrix in transforms.values())


def test_normal_warp_selects_candidate_only_after_native_and_fused_validation(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import warp as fused
  calls = []
  def reference(**kw):
    return NS(numpy=lambda: np.zeros(SHAPE, np.uint8))
  def tensor(*a, **kw):
    return NS(realize=lambda: object())
  monkeypatch.setitem(sys.modules, 'tinygrad', NS(Device={'QCOM': NS(device='QCOM')}, Tensor=tensor, TinyJit=lambda fn: reference))
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.compile_modeld',
                      NS(NV12Frame=lambda *a: object(), make_warp=lambda *a: None))
  monkeypatch.setitem(sys.modules, 'openpilot.system.hardware', NS(HARDWARE=NS(get_device_type=lambda: 'mici')))
  def validate(*a):
    calls.append('fused_validation')
    return reference
  selected = object()
  def select(*a):
    calls.append('coherent_validation')
    return selected, True
  monkeypatch.setattr(fused, 'validated_warp', validate)
  monkeypatch.setattr(coherent, 'candidate', select)
  warp = model.Warp(1344, 760, input_buffers=({}, {}))
  assert calls == ['fused_validation', 'coherent_validation']
  assert warp.coherent_output and warp.run_warp is selected


@pytest.mark.parametrize('cached', [True, False])
def test_preparation_serializes_actual_coherent_admission(monkeypatch, tmp_path, cached):
  writes = []
  def tensor(*a, **kw):
    return NS(realize=lambda: object())
  def run(**kw):
    return NS(numpy=lambda: np.zeros(SHAPE, np.uint8))
  warp = object.__new__(model.Warp)
  warp.size, warp.coherent_output, warp.run_warp, warp.inputs = 32, cached, run, {}
  save = model.Warp.save_prepared
  def dump(value, file):
    writes.append(value)
    file.write(b'mock')
  monkeypatch.setitem(sys.modules, 'tinygrad', NS(Tensor=tensor))
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.helpers', NS(dump_oob=dump))
  monkeypatch.setattr(model, 'Warp', lambda *a: NS(run_warp=run, inputs={}))
  save(warp, tmp_path / 'prepared', 1344, 760)
  assert writes[0]['coherent_output'] is cached
  assert writes[0]['identity'] == (1344, 760, 32, model.SPEC.sha256)
