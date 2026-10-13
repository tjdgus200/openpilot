"""Boot-only receiver. The PC-provisioned key is never supplied by the USB peer."""
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

import relay_manifest as wire
from local_release import owned


def verify(root, manifest):
  key = root / 'trust/comma-relay-public.pem'
  for directory in (root, key.parent):
    owned(directory, directory=True)
  owned(key)
  return wire.verify(manifest, key.read_text())


def release_path(root, manifest):
  return root / 'personal/releases' / wire.identity(manifest)


def cache_path(root, manifest):
  return root / 'personal/cache' / wire.identity(manifest)


def directory(path):
  path.mkdir(mode=0o755, exist_ok=True)
  owned(path, directory=True)
  return path


class Receiver:
  def __init__(self, root, manifest):
    verify(root, manifest)
    self.root, self.manifest = root, manifest
    self.id = wire.identity(manifest)
    base = directory(root / 'personal')
    self.path = directory(directory(base / 'incoming') / self.id)
    needed = 2 * sum(manifest[n]['size'] for n in wire.ASSETS) + (4 << 30)
    if shutil.disk_usage(root).free < needed:
      raise OSError('Insufficient DATA space; retained official release will not be removed')
    self.complete = False

  def status(self):
    offsets = []
    for name in wire.ASSETS:
      path = self.path / (name + '.part')
      if path.exists() or path.is_symlink():
        owned(path)
        if path.stat().st_size > self.manifest[name]['size']:
          raise ValueError('Oversized partial relay file')
      offsets.append(path.stat().st_size if path.exists() else 0)
    return {'id': self.id, 'offsets': offsets, 'complete': self.complete}

  def chunk(self, payload):
    if self.complete or not wire.HEADER.size < len(payload) <= wire.HEADER.size + wire.CHUNK_BYTES:
      raise ValueError('Invalid relay chunk length/state')
    digest, index, offset = wire.HEADER.unpack_from(payload)
    if digest.hex() != self.id or index >= len(wire.ASSETS):
      raise ValueError('Chunk belongs to another release')
    name = wire.ASSETS[index]
    current = self.status()['offsets'][index]
    data = payload[wire.HEADER.size:]
    if offset != current or offset + len(data) > self.manifest[name]['size']:
      raise ValueError('Out-of-order or oversized relay chunk')
    path = self.path / (name + '.part')
    # The root-owned, non-writable parent prevents unprivileged path replacement.
    with path.open('ab') as target:
      target.write(data)
      target.flush()
    path.chmod(0o644)
    return self.status()

  def finish(self):
    import update_host as update
    verify(self.root, self.manifest)
    for name in wire.ASSETS:
      path, item = self.path / (name + '.part'), self.manifest[name]
      owned(path)
      if path.stat().st_size != item['size'] or update.digest(path) != item['sha256']:
        # Permit a clean retry of this signed object after corruption/power loss.
        path.unlink()
        raise ValueError('Relay asset checksum/length mismatch')
      with path.open('rb') as source:
        os.fsync(source.fileno())
    self.complete = True
    return self.status()


def stage(root, manifest, progress=None):
  import update_host as update
  verify(root, manifest)
  update.validate_storage_compatibility(manifest)
  update.check_runtime(manifest)
  receiver = Receiver(root, manifest)
  receiver.finish()
  releases = directory(root / 'personal/releases')
  release = release_path(root, manifest)
  if release.is_symlink():
    raise ValueError('Invalid personal release symlink')
  if not release.exists():
    with tempfile.TemporaryDirectory(dir=releases, prefix='staging-') as name:
      candidate = Path(name)
      with tarfile.open(receiver.path / 'bundle.part') as archive:
        members = []
        total = 0
        for member in archive:
          members.append(member)
          total += member.size
          if len(members) > 30000 or total > 512 << 20:
            raise ValueError('Relay bundle expansion limit exceeded')
        # Personal bundles contain data and regular source files only. Bound
        # decompression and reject ALL links, duplicate paths and privilege bits.
        if len(members) > 30000 or sum(m.size for m in members) > 512 << 20:
          raise ValueError('Relay bundle expansion limit exceeded')
        seen = set()
        for member in members:
          path = candidate / member.name
          if (not (member.isfile() or member.isdir()) or member.mode & 0o7022 or
              not path.resolve().is_relative_to(candidate.resolve()) or path.resolve() in seen):
            raise ValueError('Unsafe relay archive member')
          seen.add(path.resolve())
        archive.extractall(candidate, members=members)
      if (candidate / '.comma-relay.json').exists():
        raise ValueError('Reserved relay metadata in archive')
      update.check_bundle(candidate, manifest)
      update.atomic_json(candidate / '.comma-relay.json', manifest)
      candidate.chmod(0o755)
      os.rename(candidate, release)
  owned(release, directory=True)
  if json.loads((release / '.comma-relay.json').read_text()) != manifest:
    raise ValueError('Personal release identity collision')
  caches = directory(root / 'personal/cache')
  cache = caches / wire.identity(manifest)
  models = cache / 'models'
  for path in (cache, models):
    if path.is_symlink():
      raise ValueError('Invalid personal cache symlink')
    path.mkdir(mode=0o755, exist_ok=True)
  target = models / (manifest['model']['sha256'][:16] + '.onnx')
  if target.is_symlink():
    raise ValueError('Invalid personal model symlink')
  if not target.is_file() or target.stat().st_size != manifest['model']['size'] or update.digest(target) != manifest['model']['sha256']:
    with tempfile.NamedTemporaryFile(dir=models, delete=False) as output:
      temporary = Path(output.name)
      try:
        with (receiver.path / 'model.part').open('rb') as source:
          shutil.copyfileobj(source, output, 4 << 20)
        output.flush()
        os.fsync(output.fileno())
      except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.chmod(0o644)
    os.replace(temporary, target)
  import pwd
  owner = pwd.getpwnam('jetlink')
  # Only this release's engine/cache is writable by the runtime; signed sources
  # and the incoming catalog remain root-owned. Official cache is untouched.
  for path in (cache, models, target):
    os.chown(path, owner.pw_uid, owner.pw_gid)
  update.atomic_json(root / 'updates/pending.json', manifest)
  if progress:
    progress(1.)
  os.sync()


def verify_staged(root, manifest):
  import update_host as update
  verify(root, manifest)
  update.check_runtime(manifest)
  release = release_path(root, manifest)
  owned(release, directory=True)
  if json.loads((release / '.comma-relay.json').read_text()) != manifest:
    raise ValueError('Personal staged release identity changed')
  update.check_bundle(release, manifest)
  model = cache_path(root, manifest) / 'models' / (manifest['model']['sha256'][:16] + '.onnx')
  if model.is_symlink() or model.stat().st_size != manifest['model']['size'] or update.digest(model) != manifest['model']['sha256']:
    raise ValueError('Personal staged model changed')
  return manifest
