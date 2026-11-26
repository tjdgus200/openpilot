import io
import platform
from types import SimpleNamespace

import numpy as np
import pytest
from tinygrad.helpers import Context

from openpilot.selfdrive.modeld import internal_daylight as daylight
from openpilot.selfdrive.modeld.daylight_luma import apply_daylight75_with_exponents


@pytest.mark.parametrize('main_wide,extra,expected', [
  (False, False, (True, True)), (False, True, (True, False)), (True, False, (False, False)),
])
def test_physical_camera_controls(main_wide, extra, expected):
  settings = {'ModelRoadDaylight75': True, 'ModelWideRoadDaylight75': False}
  assert daylight.daylight75_crops(SimpleNamespace(get_bool=settings.__getitem__), main_wide, extra) == expected


@pytest.mark.parametrize('enabled', [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize('dark', [True, False])
def test_streaming_native_path_equals_cached_path(enabled, dark):
  samples = np.random.default_rng(733).integers(0, 256, daylight.PACKED_SHAPE, dtype=np.uint8)
  if dark:
    samples[:, 0, ::2, ::2] %= 70
  other = samples.copy()
  assert apply_daylight75_with_exponents(samples, *enabled) == apply_daylight75_with_exponents(other, *enabled, streaming_mapping=True)
  np.testing.assert_array_equal(samples, other)


@pytest.mark.parametrize('mode,coherent,accepted,streaming', [
  (0, False, True, True), (1, False, True, True), (3, True, True, False), (3, False, False, False),
])
def test_kgsl_contract_and_host_fence(monkeypatch, mode, coherent, accepted, streaming):
  from tinygrad import dtypes
  raw = bytearray(daylight.PACKED_BYTES)
  events = []
  def mapped(force_zero_copy):
    assert force_zero_copy
    events.append('wait')
    return memoryview(raw)
  flags = (mode << 26) | (int(coherent) << 31)
  buf = SimpleNamespace(nbytes=len(raw), device='QCOM', options=None, allocator=SimpleNamespace(_as_buffer=True),
                        _buf=SimpleNamespace(meta=(SimpleNamespace(flags=flags), True)), is_initialized=lambda: True, as_memoryview=mapped)
  warped = SimpleNamespace(device='QCOM', shape=daylight.PACKED_SHAPE, dtype=dtypes.uint8, uop=SimpleNamespace(has_buffer_identity=lambda: True, buffer=buf))
  def apply(images, main, extra, streaming_mapping):
    assert images.shape == daylight.PACKED_SHAPE and (main, extra) == (True, False)
    events.append(('lut', streaming_mapping))
    return (0.5, 1.0)
  monkeypatch.setattr(daylight, 'apply_daylight75_with_exponents', apply)
  monkeypatch.setattr(daylight, 'fence_daylight75_writes', lambda: events.append('fence'))
  state = daylight.InternalDaylight75()
  state.set_crops((True, False))
  if accepted:
    assert state.apply(warped) == (0.5, 1.0)
    assert events == ['wait', ('lut', streaming), 'fence']
  else:
    assert state.apply(warped) is None
    assert not events and state.unavailable and state.crops is None


@pytest.mark.skipif(platform.machine().lower() not in ('x86_64', 'amd64'), reason='local x86 CPU JIT replay')
@Context(DEV='CPU:X86')
def test_serialized_cpu_warp_changes_policy_history_before_sampling(monkeypatch):
  from tinygrad import Device, Tensor, TinyJit
  from openpilot.selfdrive.modeld import compile_modeld as compiler
  from openpilot.selfdrive.modeld.helpers import dump_oob, load_oob
  monkeypatch.setattr(compiler, 'WARP_DEV', Device.DEFAULT)
  layout = compiler.NV12Frame(64, 40, 64, 40, 20, 64*60)
  warp = TinyJit(compiler.make_warp(layout, 512, 256, 2), prune=True)
  shapes = {'img': (1, 12, 128, 256), 'big_img': (1, 12, 128, 256),
            'desire_pulse': (1, 2, 8), 'traffic_convention': (1, 2), 'action_t': (1, 2), 'features_buffer': (1, 1, 2)}
  queues, values = compiler.make_input_queues(shapes, 2, device=Device.DEFAULT)
  for key in ('tfm', 'big_tfm'):
    values[key][:] = np.eye(3)
  frames = {key: Tensor.zeros(layout.size, dtype='uint8').contiguous().realize() for key in ('frame', 'big_frame')}
  def current():
    return warp(**{k: queues[k] for k in compiler.WARP_INPUTS}, **frames)
  def camera(frame, y, uv):
    pixels = np.full(layout.size, uv, np.uint8)
    pixels[:layout.width * layout.height] = y
    frame._buffer().copyin(memoryview(pixels))
  def camera_probe(inputs):
    return {'probe': inputs['img'].stack(inputs['big_img']).contiguous()}
  policy = TinyJit(compiler.make_run_policy(camera_probe, {'input_shapes': shapes}, 2))
  def infer(warped):
    return policy(warped, **{k: queues[k] for k in compiler.POLICY_INPUTS})[0].numpy()
  for _ in range(3):
    infer(current())
  restored = []
  for jit in (warp, policy):
    serialized = io.BytesIO()
    dump_oob(jit, serialized)
    serialized.seek(0)
    restored.append(load_oob(serialized))
  warp, policy = restored

  state = daylight.InternalDaylight75()
  first = None
  for index, flags in enumerate(((True, False), (False, True), (False, False))):
    camera(frames['frame'], 30 + index, 151)
    camera(frames['big_frame'], 45 + index, 103)
    warped = current()
    expected = warped.numpy().copy()
    exponents = apply_daylight75_with_exponents(expected, *flags)
    state.set_crops(flags)
    if state.crops:
      assert state.apply(warped) == exponents
    result = infer(warped)
    np.testing.assert_array_equal(result[:, 0, 6:], expected)
    if first is None:
      first = expected
    elif index == 2:
      np.testing.assert_array_equal(result[:, 0, :6], first)
