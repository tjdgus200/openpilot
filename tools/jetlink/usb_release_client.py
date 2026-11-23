"""Comma-side hash-pinned asset download and stop-and-wait boot-gate USB upload."""
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
import urllib.request

from tools.jetlink.usb_release import ACK, BEGIN, BEGIN_RESUME, BLOCK, CHECK, CHUNK, FINISH, HEADER, KINDS, NAMES, validate  # noqa: TID251


class PermissionLost(ConnectionError):
  """Stop file writes and wait in the same boot session for renewed permission."""


def require_permission(allowed):
  if not allowed():
    raise PermissionLost('USB update requires offroad or fresh Park/zero-speed state and USB connection')


def asset(item, cache, allowed, progress=None):
  cache.mkdir(parents=True, exist_ok=True)
  path = cache / item['sha256']
  last = [None, 0.]
  def report(stage, count):
    now = time.monotonic()
    if progress is not None and (stage != last[0] or count in (0, item['size']) or now - last[1] >= .5):
      progress(stage, count)
      last[:] = [stage, now]
  def hash_file(file):
    digest, count = hashlib.sha256(), 0
    report('checking_files', count)
    with file.open('rb') as stream:
      while data := stream.read(1 << 20):
        require_permission(allowed)
        digest.update(data)
        count += len(data)
        report('checking_files', count)
    return digest, count

  size = item['size']
  if path.is_file() and path.stat().st_size == size:
    digest, count = hash_file(path)
    if count == size and digest.hexdigest() == item['sha256']:
      return path
  partial = path.with_suffix('.part')
  if partial.is_symlink():
    raise ValueError('Invalid partial USB update asset')
  keep_partial = False
  try:
    require_permission(allowed)
    digest, count = hashlib.sha256(), 0
    if partial.is_file() and 0 < partial.stat().st_size <= size:
      digest, count = hash_file(partial)
      if count == size and digest.hexdigest() == item['sha256']:
        require_permission(allowed)
        with partial.open('r+b') as output:
          os.fsync(output.fileno())
        os.replace(partial, path)
        return path
      if not 0 < count < size:
        partial.unlink()
        digest, count = hashlib.sha256(), 0
    request = item['url']
    if count:
      request = urllib.request.Request(request, headers={'Range': f'bytes={count}-', 'Accept-Encoding': 'identity'})
    report('downloading', count)
    with urllib.request.urlopen(request, timeout=30) as source:
      if not source.url.startswith('https://'):
        raise ValueError('Insecure asset redirect')
      if count:
        if source.status == 200:
          # Servers without Range support can return the whole object.
          digest, count = hashlib.sha256(), 0
        elif source.status != 206 or source.headers.get('Content-Range') != f'bytes {count}-{size - 1}/{size}':
          raise ValueError('Invalid USB update download range')
      report('downloading', count)
      with partial.open('ab' if count else 'wb') as output:
        while data := source.read(BLOCK):
          require_permission(allowed)
          count += len(data)
          if count > size:
            raise ValueError('Oversized USB update asset')
          digest.update(data)
          output.write(data)
          report('downloading', count)
        output.flush()
        os.fsync(output.fileno())
    if count != size:
      raise ConnectionError('USB update download interrupted before expected size')
    if digest.hexdigest() != item['sha256']:
      raise ValueError('USB update download hash mismatch')
    require_permission(allowed)
    os.replace(partial, path)
    return path
  except (ConnectionError, TimeoutError, URLError, IncompleteRead) as error:
    retryable = not isinstance(error, HTTPError) or error.code in (408, 429) or 500 <= error.code < 600
    keep_partial = retryable and partial.is_file() and 0 < partial.stat().st_size <= size
    raise
  finally:
    if not keep_partial:
      partial.unlink(missing_ok=True)


def exchange(client, kind, payload, allowed, raw=False, timeout=30):
  require_permission(allowed)
  seq = client._next_seq()
  client.t.send(kind, seq, [payload if raw else json.dumps(payload).encode()], timeout=timeout)
  response = client.t.recv(timeout=timeout)
  if response.msg_type != ACK or response.seq != seq or len(response.payload) > 65536:
    raise ValueError('Unexpected USB update acknowledgement')
  value = json.loads(bytes(response.payload))
  if not isinstance(value, dict):
    raise ValueError('Invalid USB update acknowledgement')
  if value.get('ok') is not True:
    stage = {BEGIN: 'begin', BEGIN_RESUME: 'begin', CHECK: 'check', CHUNK: 'transfer', FINISH: 'verify'}[kind]
    raise ValueError(f'Jetson rejected USB update [{stage}]')
  return value


def available(client, manifest, connected):
  """Read-only check; a matching current commit alone is not registration."""
  validate(manifest)
  return exchange(client, CHECK, manifest, connected, timeout=120).get('complete') is True


def upload(client, manifest, cache, allowed, progress, *, resume=False):
  validate(manifest)
  def request(kind, payload, **kwargs):
    require_permission(allowed)
    return exchange(client, kind, payload, allowed, **kwargs)
  # An already registered package needs no network. BEGIN cannot activate it.
  progress('checking_usb', None)
  result = request(BEGIN_RESUME if resume else BEGIN, manifest, timeout=120)
  if result.get('complete') is True:
    return
  sizes = [manifest[k]['size'] for k in KINDS]
  offsets = result.get('offsets') if resume else [0] * len(KINDS)
  if (not isinstance(offsets, list) or len(offsets) != len(sizes) or
      any(type(offset) is not int or not 0 <= offset <= size for offset, size in zip(offsets, sizes, strict=True))):
    raise ValueError('Invalid USB resume offsets')
  total, sent, prepared = sum(sizes), sum(offsets), 0
  paths = []
  # Never leave the receiver between bundle/model while fetching a large model.
  # Verified cached files still get hash checks; registered packages use BEGIN only.
  for kind, size, offset in zip(KINDS, sizes, offsets, strict=True):
    paths.append(None if offset == size else asset(manifest[kind], Path(cache), allowed,
                                                  progress=lambda stage, count, base=prepared: progress(stage, (base + count) / total)))
    prepared += size
  progress('transferring', sent / total)
  for index, (path, offset, size) in enumerate(zip(paths, offsets, sizes, strict=True)):
    if path is None:
      continue
    with path.open('rb') as source:
      source.seek(offset)
      while data := source.read(BLOCK):
        result = request(CHUNK, HEADER.pack(index, offset) + data, raw=True)
        offset += len(data)
        sent += len(data)
        if result.get('index') != index or result.get('offset') != offset:
          raise ValueError('USB acknowledgement offset mismatch')
        progress('transferring', sent / total)
    if offset != size:
      raise ValueError('Cached USB asset changed during transfer: ' + NAMES[index])
  progress('verifying', 1.)
  if request(FINISH, {}, timeout=120).get('complete') is not True:
    raise ValueError('USB update registration incomplete')
