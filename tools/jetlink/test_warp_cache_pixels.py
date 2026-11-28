"""Exercise serialized camera warps with real CPU TinyJit kernels."""
import io
import platform
import sys

import numpy as np
import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import get_warp_matrix, wide_model_pitch, wider_model_warp
from openpilot.selfdrive.modeld.jetlink.warp_cache import reference_pixels, verify_cached
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info


pytestmark = pytest.mark.skipif(sys.platform != 'linux' or platform.machine() != 'x86_64',
                               reason='requires the tinygrad Linux x86 CPU renderer')


def nv12_frame(rng, layout):
  width, height, stride, y_height, _, size = layout
  frame = np.full(size, 255, np.uint8)
  uv_offset = stride * y_height
  y = frame[:height * stride].reshape(height, stride)
  uv = frame[uv_offset:uv_offset + height // 2 * stride].reshape(height // 2, stride)
  y[:, :width] = rng.integers(0, 255, (height, width), dtype=np.uint8)
  uv[:, :width] = rng.integers(0, 255, (height // 2, width), dtype=np.uint8)
  return frame


@pytest.mark.parametrize('device_camera', [('tizi', 'ox03c10'), ('mici', 'os04c10')])
@pytest.mark.parametrize('fused', [False, True], ids=['original', 'fused'])
def test_cold_and_cached_warp_pixels(monkeypatch, device_camera, fused):
  tinygrad = pytest.importorskip('tinygrad')
  from openpilot.selfdrive.modeld import compile_modeld
  from openpilot.selfdrive.modeld.helpers import dump_oob, load_oob
  from openpilot.selfdrive.modeld.jetlink import warp

  cameras = DEVICE_CAMERAS[device_camera]
  width, height = cameras.fcam.size
  layout = compile_modeld.NV12Frame(width, height, *get_nv12_info(width, height))
  assert layout.stride > width and layout.y_height > height and layout.uv_height > height // 2
  make_warp = warp.make_warp if fused else compile_modeld.make_warp
  monkeypatch.setattr(compile_modeld, 'WARP_DEV', 'CPU')
  with tinygrad.Context(DEV='CPU:X86'):
    matrices = [np.eye(3, dtype=np.float32), np.eye(3, dtype=np.float32)]
    tfm, big_tfm = (tinygrad.Tensor(value, device='NPY').realize() for value in matrices)
    cold = tinygrad.TinyJit(make_warp(layout, 512, 256, 2))
    rng = np.random.default_rng(7527)
    for _ in range(3):
      frames = [tinygrad.Tensor(nv12_frame(rng, layout), device='CPU').realize() for _ in matrices]
      cold(tfm=tfm, big_tfm=big_tfm, frame=frames[0], big_frame=frames[1]).numpy()
    artifact = io.BytesIO()
    dump_oob(cold, artifact)
    artifact.seek(0)
    restored = load_oob(artifact)

    cases = [(0, 0, [0, 0, 0]), (80, 70, [.006, -.011, .016]),
             (65, 100, [.009, .002, .027]), (0, 0, [.009, .002, .027])]
    for fov, down, rpy in cases:
      euler = np.array(rpy, np.float32)
      matrices[0][:] = get_warp_matrix(euler, cameras.fcam.intrinsics)
      wide = get_warp_matrix(wide_model_pitch(euler, down), cameras.ecam.intrinsics, True)
      matrices[1][:] = wider_model_warp(wide, fov) if fov else wide
      frames = [tinygrad.Tensor(nv12_frame(rng, layout), device='CPU').realize() for _ in matrices]
      inputs = {'tfm': tfm, 'big_tfm': big_tfm, 'frame': frames[0], 'big_frame': frames[1]}
      expected = cold(**inputs).numpy().copy()
      actual = restored(**inputs).numpy()
      assert actual.shape == (2, 6, 128, 256) and actual.dtype == np.uint8
      np.testing.assert_array_equal(actual, expected)
      assert not np.any(actual == 255)

    # Independent NV12 coordinates, with exact affine terms and clipped borders.
    matrices[0][:] = [[2, 0, -12], [0, -2, height + 8], [0, 0, 1]]
    matrices[1][:] = [[-2, 0, width + 8], [0, 2, -12], [0, 0, 1]]
    pixels = [nv12_frame(rng, layout) for _ in matrices]
    frames = [tinygrad.Tensor(value, device='CPU').realize() for value in pixels]
    actual = restored(tfm=tfm, big_tfm=big_tfm, frame=frames[0], big_frame=frames[1]).numpy()
    expected = np.stack([reference_pixels(value, matrix, layout) for value, matrix in zip(pixels, matrices, strict=True)])
    np.testing.assert_array_equal(actual, expected)


def test_cache_probe_rejects_prior_frame_even_with_valid_first_output(monkeypatch):
  from types import SimpleNamespace as NS
  from openpilot.selfdrive.modeld.jetlink import model

  width, height = 1928, 1208
  layout = (width, height, *get_nv12_info(width, height))
  transforms = {name: np.empty((3, 3), np.float32) for name in ('tfm', 'big_tfm')}
  outputs = []

  class Frame:
    def __init__(self, data, device):
      assert device == 'QCOM'
      self.data = data

    def realize(self):
      return self

    def _buffer(self):
      return self

    def copyin(self, data):
      self.data = np.frombuffer(data, dtype=np.uint8)

  def stale_warp(frame, big_frame):
    if not outputs:
      outputs.append(np.stack([reference_pixels(frame.data, transforms['tfm'], layout),
                               reference_pixels(big_frame.data, transforms['big_tfm'], layout)]))
    return NS(numpy=lambda: outputs[0])

  warp = NS(inputs={}, transforms=transforms, run_warp=stale_warp)
  monkeypatch.setitem(sys.modules, 'tinygrad', NS(Device={'QCOM': NS(synchronize=lambda: None)}, Tensor=Frame))
  monkeypatch.setattr(model, 'Warp', lambda width, height, prepared: warp)
  with pytest.raises(ValueError, match='cached warp pixel validation failed'):
    verify_cached(width, height, b'capture')
