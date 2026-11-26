"""Apply the camera-specific Y75 curve to the internal model's current warped pair."""

try:
  from openpilot.selfdrive.modeld.daylight_luma import apply_daylight75_with_exponents, fence_daylight75_writes
except ImportError:
  apply_daylight75_with_exponents = None
  fence_daylight75_writes = None

PACKED_SHAPE = (2, 6, 128, 256)
PACKED_BYTES = 2 * 6 * 128 * 256


def daylight75_crops(params, main_wide_camera, use_extra_client):
  """Resolve physical camera controls even when both crops use the same stream."""
  road = params.get_bool('ModelRoadDaylight75')
  wide = params.get_bool('ModelWideRoadDaylight75')
  return (wide if main_wide_camera else road, wide if main_wide_camera or use_extra_client else road)


class InternalDaylight75:
  def __init__(self):
    self.crops = None
    self.unavailable = False
    self.staging = None

  def disable(self, reason):
    from openpilot.common.swaglog import cloudlog
    cloudlog.warning('Internal daylight Y75 unavailable: %s', reason)
    self.unavailable = True
    self.crops = None

  def set_crops(self, crops):
    self.crops = crops if any(crops) and not self.unavailable else None
    if self.crops is not None and apply_daylight75_with_exponents is None:
      self.disable('native module missing')

  def apply(self, warped):
    from tinygrad import dtypes
    # A compiled warp returns a realized packed Tensor. Do not materialize a
    # contiguous copy here: run_policy must consume this exact buffer.
    if (not isinstance(warped.device, str) or tuple(warped.shape) != PACKED_SHAPE or warped.dtype != dtypes.uint8 or
        not warped.uop.has_buffer_identity()):
      return self.disable('unsupported packed warp tensor')
    buf = warped.uop.buffer
    if buf.nbytes != PACKED_BYTES or not buf.is_initialized():
      return self.disable('invalid warp allocation')
    dev = buf.device.split(':', 1)[0]
    if dev in ('QCOM', 'CPU'):
      if buf.options is not None or not hasattr(buf.allocator, '_as_buffer'):
        return self.disable('warp output does not support synchronized CPU mapping')
      streaming_mapping = False
      if dev == 'QCOM':
        # Accept only KGSL-allocated, CPU-writable WC/uncached or I/O-coherent
        # storage. Unmapped/external or cached noncoherent memory needs a
        # different cache maintenance contract.
        from tinygrad.runtime.autogen import kgsl
        meta = buf._buf.meta
        if not meta or len(meta) != 2 or not meta[1] or not hasattr(meta[0], 'flags'):
          return self.disable('warp output is not KGSL-owned')
        flags = meta[0].flags
        mode = (flags & kgsl.KGSL_CACHEMODE_MASK) >> kgsl.KGSL_CACHEMODE_SHIFT
        streaming_mapping = mode in (kgsl.KGSL_CACHEMODE_WRITECOMBINE, kgsl.KGSL_CACHEMODE_UNCACHED)
        if not streaming_mapping and not flags & (1 << 31):
          return self.disable('noncoherent cached warp allocation')
      # Both pinned allocators synchronize before handing out this view. It
      # refers to the current offset/extent, and lives only while warped is held.
      try:
        raw = buf.as_memoryview(force_zero_copy=True)
        if raw.readonly or not raw.c_contiguous or raw.nbytes != PACKED_BYTES:
          return self.disable('invalid mapped warp extent')
        images = raw.cast('B', PACKED_SHAPE)
      except (AssertionError, BufferError, TypeError, ValueError) as exc:
        return self.disable(f'incompatible warp mapping ({type(exc).__name__})')
      exponents = apply_daylight75_with_exponents(images, *self.crops, streaming_mapping=streaming_mapping)
      if dev == 'QCOM' and exponents != (1.0, 1.0):
        fence_daylight75_writes()
      return exponents

    # Discrete PC backends expose host/device transfers, not a coherent CPU
    # mapping. Reuse the same staging allocation across frames and toggles.
    if self.staging is None:
      self.staging = memoryview(bytearray(PACKED_BYTES))
    buf.copyout(self.staging)
    exponents = apply_daylight75_with_exponents(self.staging.cast('B', PACKED_SHAPE), *self.crops)
    if exponents != (1.0, 1.0):
      buf.copyin(self.staging)
    return exponents
