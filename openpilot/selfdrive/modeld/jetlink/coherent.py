"""Optional QCOM output caching; never change a linked JIT's buffer address."""
import io
import os

import numpy as np

# KGSL_MEMFLAGS_IOCOHERENT | KGSL_CACHEMODE_WRITEBACK. Check the kernel's answer,
# not merely the requested bits. Based on official Jetlink v0.8.12 warp.py.
COHERENT_WRITEBACK = (1 << 31) | (3 << 26)


def enabled():
  return os.getenv('JETLINK_COHERENT_WARP', '1') != '0'


def install(jit, shape):
  """Only an unlinked deserialized capture may replace its output allocation."""
  from tinygrad.device import Device
  captured = jit.captured
  if (captured is None or 'linear' in vars(captured) or
      not all(hasattr(captured, name) for name in ('_linear', 'expected_names', 'expected_input_info'))):
    raise ValueError('coherent warp requires an unlinked capture')
  result = captured.ret
  output = result.uop.base.buffer
  if (tuple(result.shape) != tuple(shape) or output.nbytes != int(np.prod(shape)) or
      not output.device.startswith('QCOM') or getattr(output, '_base', None) is not None):
    raise ValueError('unsupported coherent warp output')
  device = Device[output.device]
  # Restoring serialized buffers may have submitted copies. Finish before
  # retiring the old allocation; this is startup only, never per-frame work.
  device.synchronize()
  memory = device._gpu_alloc(output.nbytes, flags=COHERENT_WRITEBACK)
  try:
    if memory.meta[0].flags & COHERENT_WRITEBACK != COHERENT_WRITEBACK:
      raise ValueError('coherent warp flags unsupported')
    output.deallocate()
    output.allocate(opaque=memory)
  except Exception:
    # If allocate transferred ownership before raising, the Buffer owns cleanup.
    if not output.is_allocated() or output._buf is not memory:
      device._gpu_free(memory)
    raise


def candidate(reference, nv12, transforms, inputs):
  """Validate a private unlinked copy in the non-realtime preparation process."""
  if not enabled():
    return reference, False
  from tinygrad import Tensor
  from openpilot.common.swaglog import cloudlog
  from openpilot.selfdrive.modeld.helpers import dump_oob, load_oob
  from openpilot.selfdrive.modeld.jetlink.warp import validation_matrices
  submitted = False
  try:
    serialized = io.BytesIO()
    dump_oob(reference, serialized)
    proposed = load_oob(io.BytesIO(serialized.getvalue()))
    shape = (2, 6, 128, 256)
    install(proposed, shape)
    rng = np.random.default_rng(32167)
    submitted = True
    frames = {key: Tensor(np.zeros(nv12.size, np.uint8), device='QCOM').realize()
              for key in ('frame', 'big_frame')}
    # Distinct frames and transforms make a cached previous result fail parity.
    for index, matrix in enumerate(validation_matrices()):
      for frame in frames.values():
        frame._buffer().copyin(memoryview(rng.integers(0, 256, nv12.size, dtype=np.uint8)))
      transforms['tfm'][:] = matrix
      transforms['big_tfm'][:] = np.asarray(matrix) * np.array([[1.01, 1, 1], [1, .99, 1], [1, 1, 1]])
      expected = reference(**inputs, **frames).numpy()
      actual = proposed(**inputs, **frames).numpy()  # Keep the completion fence.
      if actual.dtype != np.uint8 or actual.shape != shape or not np.array_equal(actual, expected):
        raise ValueError(f'coherent warp pixel validation failed at probe {index}')
    cloudlog.warning('Jetlink coherent warp verified: %d probes', len(validation_matrices()))
    return proposed, True
  except Exception:
    cloudlog.exception('Jetlink coherent warp unavailable; retaining original camera warp')
    return reference, False
  finally:
    for matrix in transforms.values():
      matrix[:] = np.eye(3, dtype=np.float32)
    if submitted:
      # Also drain a submission whose numpy()/validation raised. If the device
      # cannot finish, preparation fails instead of exporting an unsafe capture.
      from tinygrad.device import Device
      Device['QCOM'].synchronize()
