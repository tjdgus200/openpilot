"""A single private, device-bound slot for a locally validated QCOM warp capture."""
from contextlib import ExitStack, contextmanager, suppress
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat

CACHE = Path('/data/jetlink-warp-cache')
MAX_ARTIFACT = 32 << 20
ACTIVE, CHECKING, STAGING = 'warp.bin', 'checking', 'staging'
SEAL = 'local.key'


def identity(width, height):
  from tinygrad import Device
  import tinygrad
  from tinygrad.helpers import ContextVar
  from openpilot.selfdrive.modeld.jetlink.link import SPEC
  from openpilot.selfdrive.modeld.precompiled_validation import camera_sizes, device_context
  from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

  context = device_context()
  if (context['device'] not in ('mici', 'tici', 'tizi') or (width, height) not in camera_sizes(context['device'])
      or not all(context.values())):
    raise ValueError('unidentified QCOM platform or camera layout')
  gpu = Device['QCOM']
  renderer = gpu.renderer
  # KGSL is part of the AGNOS kernel; compiler libraries can also be replaced
  # independently. Use the mapped compiler's actual file identity.
  libraries = {}
  for line in Path('/proc/self/maps').read_text().splitlines():
    fields = line.split(None, 5)
    if len(fields) == 6 and fields[5].startswith('/') and fields[5] not in libraries:
      path = Path(fields[5])
      if path.name.startswith(('libllvm-qcom', 'libtinymesa')):
        info = path.stat()
        libraries[fields[5]] = (info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns, info.st_mtime_ns)
  if not gpu.gpu_id or not libraries:
    raise ValueError('unidentified QCOM driver or compiler')

  here = Path(__file__).parent
  runtime = Path(tinygrad.__file__).resolve().parent
  sources = [here / name for name in ('warp_cache.py', 'prepare.py', 'model.py', 'warp.py', 'coherent.py', 'contracts.py')]
  sources += [here.parent / name for name in ('compile_modeld.py', 'helpers.py', 'precompiled_validation.py')]
  # Bind to the files actually used, including local edits and prebuilt trees
  # without Git metadata. Hash files in a streaming pass in the isolated worker.
  sources += sorted(runtime.rglob('*.py'))
  digests = {}
  for source in sources:
    name = str(Path('tinygrad') / source.relative_to(runtime)) if source.is_relative_to(runtime) else str(source.relative_to(here.parent))
    with source.open('rb') as stream:
      digests[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
  values = (1, context, Path('/proc/version').read_text(), gpu.gpu_id, type(renderer).__qualname__, libraries,
            width, height, get_nv12_info(width, height), SPEC.sha256, SPEC.warped_shape, SPEC.frame_skip, digests,
            {k: repr(v.value) for k, v in ContextVar._cache.items()},
            {k: os.getenv(k) for k in ('WARP_DEV', 'QCOM_PRIORITY', 'JETLINK_COHERENT_WARP', 'LD_PRELOAD')})
  return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).digest()


@contextmanager
def private_slot():
  import fcntl
  flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
  with ExitStack() as stack:
    fd = os.open('/', flags)
    stack.callback(os.close, fd)
    for depth, part in enumerate(CACHE.parts[1:], start=1):
      info = os.fstat(fd)
      if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
        raise PermissionError('unsafe warp cache ancestor')
      if depth == len(CACHE.parts) - 1:
        with suppress(FileExistsError):
          os.mkdir(part, mode=0o700, dir_fd=fd)
      fd = os.open(part, flags, dir_fd=fd)
      stack.callback(os.close, fd)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
      raise PermissionError('unsafe warp cache directory')
    # A concurrent preparer simply takes the existing cold path.
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield fd


@contextmanager
def private_file(fd, name):
  # Never follow a link or block opening a FIFO. Validate the same inode from
  # which we read. Only this UID's private file may reach pickle deserialization.
  flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
  with os.fdopen(os.open(name, flags, dir_fd=fd), 'rb') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
      raise PermissionError('unsafe warp cache file')
    yield stream, info.st_size


def local_seal(fd, *, create=False):
  try:
    with private_file(fd, SEAL) as (stream, size):
      if size == 32 and len(secret := stream.read(33)) == 32:
        return secret
      raise ValueError('incomplete warp cache seal')
  except (ValueError, OSError):
    if not create:
      raise
  # Only issue a seal after a successful locally compiled and validated warp.
  # A killed first write is rejected, then replaced by the next cold build.
  with suppress(FileNotFoundError):
    os.unlink(SEAL, dir_fd=fd)
  flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
  secret = os.urandom(32)
  with os.fdopen(os.open(SEAL, flags, mode=0o600, dir_fd=fd), 'wb') as stream:
    stream.write(secret)
    stream.flush()
    os.fsync(stream.fileno())
  return secret


def read_entry(fd, key):
  with private_file(fd, ACTIVE) as (stream, total):
    size = total - 64
    if not 0 < size <= MAX_ARTIFACT or stream.read(32) != key:
      raise ValueError('ineligible cached warp')
    checksum = stream.read(32)
    artifact = stream.read(size)
    digest = hmac.new(local_seal(fd), key, 'sha256')
    digest.update(artifact)
    if len(artifact) != size or stream.read(1) or not hmac.compare_digest(digest.digest(), checksum):
      raise ValueError('invalid cached warp digest')
    return artifact


def store_entry(fd, key, target):
  with target.open('rb') as source:
    size = os.fstat(source.fileno()).st_size
    if not 0 < size <= MAX_ARTIFACT:
      raise ValueError('warp exceeds cache slot')
    with suppress(FileNotFoundError):
      os.unlink(STAGING, dir_fd=fd)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(STAGING, flags, mode=0o600, dir_fd=fd), 'wb') as out:
      out.write(key)
      out.write(bytes(32))
      digest = hmac.new(local_seal(fd, create=True), key, 'sha256')
      remaining = size
      while remaining:
        block = source.read(min(remaining, 1 << 20))
        if not block:
          raise ValueError('truncated warp')
        out.write(block)
        digest.update(block)
        remaining -= len(block)
      if source.read(1):
        raise ValueError('warp changed during write')
      out.seek(32)
      out.write(digest.digest())
      out.flush()
      os.fsync(out.fileno())
    os.replace(STAGING, ACTIVE, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def reference_pixels(frame, matrix, layout, model_w=512, model_h=256):
  """Independent nearest-neighbor NV12 reference; packed Y00,Y10,Y01,Y11,U,V."""
  import numpy as np
  width, height, stride, y_height, _, _ = layout
  def sample(chroma):
    scale = 2 if chroma else 1
    y, x = np.ogrid[:model_h // scale, :model_w // scale]
    x, y = x.astype(np.float32), y.astype(np.float32)
    m = np.asarray(matrix, dtype=np.float32)
    z = m[2, 0]*scale*x + m[2, 1]*scale*y + m[2, 2]
    sx = np.rint((m[0, 0]*x + m[0, 1]*y + m[0, 2]/scale)/z).clip(0, width//scale-1).astype(np.intp)
    sy = np.rint((m[1, 0]*x + m[1, 1]*y + m[1, 2]/scale)/z).clip(0, height//scale-1).astype(np.intp)
    return sy * stride + sx * scale + (stride * y_height if chroma else 0)
  y = frame[sample(False)]
  uv = sample(True)
  return np.stack((y[::2, ::2], y[1::2, ::2], y[::2, 1::2], y[1::2, 1::2], frame[uv], frame[uv+1]))


def verify_cached(width, height, artifact):
  import numpy as np
  from tinygrad import Device, Tensor
  from openpilot.selfdrive.modeld.jetlink.model import Warp
  from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

  layout = (width, height, *get_nv12_info(width, height))
  rng = np.random.default_rng()
  warp = Warp(width, height, prepared=artifact)
  frames = {}
  try:
    for index in range(2):
      expected = []
      for wide, (name, tfm) in enumerate((('frame', 'tfm'), ('big_frame', 'big_tfm'))):
        # Binary-exact, distinct transforms exercise clipping, NV12 stride,
        # channel order and matrix/frame updates without GPU rounding ambiguity.
        sign = 1 if index == wide else -1
        dx, dy = rng.integers(-8, 9, size=2) * 4
        matrix = [[2*sign, 0, width//2 + dx], [0, -sign, height//2 + dy], [0, 0, 1]]
        warp.transforms[tfm][:] = matrix
        pixels = rng.integers(0, 256, layout[-1], dtype=np.uint8)
        expected.append(reference_pixels(pixels, matrix, layout))
        if index == 0:
          frames[name] = Tensor(pixels, device='QCOM').realize()
        else:
          frames[name]._buffer().copyin(memoryview(pixels))
      actual = warp.run_warp(**warp.inputs, **frames).numpy()
      if actual.dtype != np.uint8 or not np.array_equal(actual, np.stack(expected)):
        raise ValueError('cached warp pixel validation failed')
  finally:
    Device['QCOM'].synchronize()


def restore_entry(fd, key, width, height, target):
  artifact = read_entry(fd, key)
  # Quarantine before executing: a crashed or killed worker cannot keep
  # selecting this capture. Only a successful pixel check restores it.
  os.replace(ACTIVE, CHECKING, src_dir_fd=fd, dst_dir_fd=fd)
  verify_cached(width, height, artifact)
  target.write_bytes(artifact)
  with suppress(OSError):
    os.replace(CHECKING, ACTIVE, src_dir_fd=fd, dst_dir_fd=fd)


def prepare(width, height, target, *, refresh=False):
  from openpilot.selfdrive.modeld.jetlink.model import Warp

  with ExitStack() as stack:
    try:
      fd = stack.enter_context(private_slot())
      key = identity(width, height)
    except Exception:
      fd = None
    if fd is not None and refresh:
      with suppress(OSError):
        os.unlink(ACTIVE, dir_fd=fd)
    if fd is not None and not refresh:
      try:
        restore_entry(fd, key, width, height, target)
        return 'hit'
      except Exception:
        pass
      finally:
        with suppress(OSError):
          os.unlink(CHECKING, dir_fd=fd)
    # This is the existing cold build, validation and serialization contract.
    Warp(width, height).save_prepared(target, width, height)
    if fd is not None:
      try:
        store_entry(fd, key, target)
        return 'rebuilt'
      except (OSError, ValueError, MemoryError):
        pass
      finally:
        with suppress(OSError):
          os.unlink(STAGING, dir_fd=fd)
    return 'uncached'
