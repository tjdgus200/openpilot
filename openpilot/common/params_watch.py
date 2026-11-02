"""Invalidate a small settings snapshot without changing Params read semantics.

Linux notifications are advisory: a lost watch, overflow, unsupported platform
or replaced directory always requires fresh reads. No worker or polling timer.
"""
import ctypes
import os
import struct
import sys


class ParamsWatch:
  def __init__(self, params, keys):
    self.fd = -1
    self.path = None
    self.identity = None
    self.keys = {key.encode() for key in keys}
    try:
      if not sys.platform.startswith('linux'):
        return
      self.path = params.get_param_path()
      self.identity = self._identity()
      libc = ctypes.CDLL(None, use_errno=True)
      self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
      if self.fd < 0:
        return
      # MODIFY also covers callers writing in place, not only Params' rename.
      mask = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
      if libc.inotify_add_watch(self.fd, os.fsencode(self.path), mask) < 0:
        self.close()
    except (AttributeError, OSError, TypeError):
      self.close()

  def _identity(self):
    value = os.stat(self.path)
    return value.st_dev, value.st_ino

  def changed(self):
    if self.fd < 0:
      return True
    try:
      if self._identity() != self.identity:
        self.close()
        return True
      try:
        data = os.read(self.fd, 65536)
      except BlockingIOError:
        return False
      if not data:
        self.close()
        return True
      changed = len(data) == 65536
      offset = 0
      while offset < len(data):
        _, mask, _, length = struct.unpack_from('iIII', data, offset)
        offset += 16
        if offset + length > len(data):
          raise ValueError('Truncated settings notification')
        name = data[offset:offset + length].split(b'\0', 1)[0]
        offset += length
        # Overflow, watch deletion/unmount or directory moves are not a cache hit.
        if mask & (0x4000 | 0x8000 | 0x2000 | 0x400 | 0x800):
          self.close()
          return True
        changed |= not name or name in self.keys
      return changed
    except (OSError, ValueError, struct.error):
      self.close()
      return True

  def close(self):
    if self.fd >= 0:
      os.close(self.fd)
      self.fd = -1

  def __del__(self):
    self.close()
