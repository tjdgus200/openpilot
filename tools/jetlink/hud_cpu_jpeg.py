"""Pinned CPU-only libjpeg-turbo encoder for the Jetson HUD."""
import hashlib
from pathlib import Path
import platform
import sys

LIBRARY = Path(__file__).with_name('jpeg_runtime') / 'libturbojpeg.so.0.2.0'
LIBRARY_SHA256 = '7aff9f95459048afcf24b4768552b4211cff65c0bf006b3be4a0dead5e75be58'


def load():
  if sys.platform != 'linux' or platform.machine() not in ('aarch64', 'arm64'):
    raise RuntimeError('Bundled CPU JPEG library requires Linux ARM64')
  if hashlib.sha256(LIBRARY.read_bytes()).hexdigest() != LIBRARY_SHA256:
    raise RuntimeError('Bundled CPU JPEG library integrity mismatch')
  from jpeg_runtime import turbojpeg
  return turbojpeg, turbojpeg.TurboJPEG(str(LIBRARY))


class Encoder:
  def __init__(self):
    self.api, self.encoder = load()

  def encode(self, rgba, width, height, quality):
    import numpy as np
    if (type(width) is not int or type(height) is not int or
        not 2 <= width <= 1920 or not 2 <= height <= 1920 or width * height > 1920 * 720 or
        width % 2 or height % 2 or type(quality) is not int or not 1 <= quality <= 95):
      raise RuntimeError('CPU JPEG dimensions/quality outside HUD bounds')
    source = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)
    result = self.encoder.encode(source, quality=quality, pixel_format=self.api.TJPF_RGBA,
                                 jpeg_subsample=self.api.TJSAMP_420)
    if (not isinstance(result, bytes) or not 4 <= len(result) <= 1024 * 1024
        or not result.startswith(b'\xff\xd8') or not result.endswith(b'\xff\xd9')):
      raise RuntimeError('CPU JPEG output outside HUD bounds')
    return result
