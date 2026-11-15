"""Load the repository's FULL cereal schema, including on Windows Unicode paths."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile


@contextmanager
def full_schema(repo=None):
  import capnp

  repo = Path(repo) if repo else Path(__file__).resolve().parents[2]
  contents = {name: (repo / 'openpilot/cereal' / name).read_bytes() for name in ('log.capnp', 'custom.capnp', 'deprecated.capnp')}
  contents['car.capnp'] = (repo / 'opendbc_repo/opendbc/car/car.capnp').read_bytes()
  contents['include/c++.capnp'] = (Path(capnp.__file__).parent / 'c++.capnp').read_bytes()
  digest = hashlib.sha256(b''.join(name.encode() + data for name, data in sorted(contents.items()))).hexdigest()
  # Capnp keeps Windows schema file mappings alive until process exit. Use a
  # content-addressed PUBLIC-schema cache instead of leaking one temp per job.
  cache = Path(tempfile.gettempdir()) / 'pv5-public-cereal-schema'
  if cache.is_symlink():
    raise ValueError('unsafe_schema_cache')
  cache.mkdir(exist_ok=True)
  if len(list(cache.iterdir())) > 16:
    raise ValueError('schema_cache_version_budget')
  root = cache / digest
  root.mkdir(exist_ok=True)
  if root.is_symlink():
    raise ValueError('unsafe_schema_cache')
  (root / 'include').mkdir(exist_ok=True)
  for name, data in contents.items():
    path = root / name
    if path.is_symlink():
      raise ValueError('unsafe_schema_cache')
    if path.exists():
      if path.read_bytes() != data:
        raise ValueError('schema_cache_identity')
      continue
    # Publish complete immutable bytes, even when multiple PC workers start together.
    fd, temporary_name = tempfile.mkstemp(prefix='schema-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
      with os.fdopen(fd, 'wb') as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
      try:
        os.link(temporary, path)
      except FileExistsError:
        if path.read_bytes() != data:
          raise ValueError('schema_cache_identity') from None
    finally:
      temporary.unlink(missing_ok=True)
  parser = capnp.SchemaParser()
  yield parser.load(str(root / 'log.capnp'), imports=[str(root)])
