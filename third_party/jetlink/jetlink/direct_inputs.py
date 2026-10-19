"""Opt-in CPU staging into fixed TensorRT buffers; existing wire bytes unchanged."""
import numpy as np

from jetlink.input_cast import native_u8_to_f16
from jetlink.packed_codec import MAP_PIXELS, _layout


class DirectInputs:
  def __init__(self, spec, dest):
    self.spec, self.dest = spec, dest
    self.convert = native_u8_to_f16()
    if self.convert is None:
      raise RuntimeError('Requested native input conversion is unavailable')
    for name in ('new_img', *spec.packed_shapes):
      value = dest[name]
      if (value.shape != spec.input_shapes[name] or value.dtype not in (np.dtype('float16'), np.dtype('float32'))
          or not value.flags.c_contiguous or not value.flags.aligned or not value.flags.writeable):
        raise ValueError(f'Unsupported direct input binding: {name}')
    self.bindings = tuple((name, dest[name], dest[name].ctypes.data, dest[name].dtype)
                          for name in ('new_img', *spec.packed_shapes))
    self.layout = None
    if 'nav_map' in spec.packed_shapes:
      _layout(spec)  # Same stateful/256x256 compact contract as the old decoder.
      offset, layout = 0, []
      for name, size in zip(spec.packed_shapes, spec.packed_sizes, strict=True):
        is_map = name == 'nav_map'
        layout.append((name, offset, size, is_map))
        offset += size if is_map else size * 4
      self.layout = tuple(layout)
      self.compact_nbytes = spec.packed_nbytes - MAP_PIXELS * 3
      if offset != self.compact_nbytes:
        raise ValueError('Invalid compact input layout')

  def copy_image(self, warped, dest):
    if dest is not self.dest or any(dest.get(name) is not value or value.ctypes.data != pointer
                                   or value.dtype != dtype or value.shape != self.spec.input_shapes[name]
                                   or not value.flags.writeable
                                   for name, value, pointer, dtype in self.bindings):
      raise ValueError('Direct input buffers changed after load')
    target = dest['new_img']
    if target.dtype == np.float16:
      self.convert(warped, target)
    else:
      target[...] = warped

  def validate_compact(self, payload):
    raw = memoryview(payload).cast('B')
    if self.layout is None or raw.nbytes != self.compact_nbytes:
      raise ValueError('Invalid compact navigation context size')
    return raw

  def compact_fields(self, payload):
    raw = self.validate_compact(payload)
    values = tuple((name, np.frombuffer(raw, np.uint8 if is_map else np.float32, size, offset), is_map)
                   for name, offset, size, is_map in self.layout)
    # The map's on-wire type already proves every pixel is finite and in
    # 0..255. Every float scalar still receives the original finite check.
    if any(not np.isfinite(value).all() for _, value, is_map in values if not is_map):
      raise ValueError('Non-finite stateful scalar input')
    return values

  def copy_fields(self, values, dest):
    for name, value, is_map in values:
      target = dest[name]
      if is_map and target.dtype == np.float16:
        self.convert(value, target)
      else:
        target[...] = value.reshape(target.shape)
