"""Comma-side hash-pinned asset download and stop-and-wait boot-gate USB upload."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

from tools.jetlink.usb_release import ACK, BEGIN, BLOCK, CHECK, CHUNK, FINISH, HEADER, KINDS, NAMES, validate  # noqa: TID251


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
  def matches():
    if not path.is_file() or path.stat().st_size != item['size']:
      return False
    digest, count = hashlib.sha256(), 0
    report('checking_files', count)
    with path.open('rb') as stream:
      while data := stream.read(1 << 20):
        require_permission(allowed)
        digest.update(data)
        count += len(data)
        report('checking_files', count)
    return digest.hexdigest() == item['sha256']
  if matches():
    return path
  partial = path.with_suffix('.part')
  try:
    require_permission(allowed)
    report('downloading', 0)
    with urllib.request.urlopen(item['url'], timeout=30) as source, partial.open('wb') as output:
      if not source.url.startswith('https://'):
        raise ValueError('Insecure asset redirect')
      count, digest = 0, hashlib.sha256()
      while data := source.read(BLOCK):
        require_permission(allowed)
        count += len(data)
        if count > item['size']:
          raise ValueError('Oversized USB update asset')
        digest.update(data)
        output.write(data)
        report('downloading', count)
      output.flush()
      os.fsync(output.fileno())
    if count != item['size'] or digest.hexdigest() != item['sha256']:
      raise ValueError('USB update download hash mismatch')
    os.replace(partial, path)
    return path
  finally:
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
    stage = {BEGIN: 'begin', CHECK: 'check', CHUNK: 'transfer', FINISH: 'verify'}[kind]
    raise ValueError(f'Jetson rejected USB update [{stage}]: ' + str(value.get('error', 'invalid reply')))
  return value


def available(client, manifest, connected):
  """Read-only check; a matching current commit alone is not registration."""
  validate(manifest)
  return exchange(client, CHECK, manifest, connected, timeout=120).get('complete') is True


def upload(client, manifest, cache, allowed, progress):
  validate(manifest)
  def request(kind, payload, **kwargs):
    require_permission(allowed)
    return exchange(client, kind, payload, allowed, **kwargs)
  # An already registered package needs no network. BEGIN cannot activate it.
  progress('checking_usb', None)
  result = request(BEGIN, manifest, timeout=120)
  if result.get('complete') is True:
    return
  total, sent, prepared = sum(manifest[k]['size'] for k in KINDS), 0, 0
  paths = []
  # Never leave the receiver between bundle/model while fetching a large model.
  # Verified cached files still get hash checks; registered packages use BEGIN only.
  for kind in KINDS:
    paths.append(asset(manifest[kind], Path(cache), allowed,
                       progress=lambda stage, count, base=prepared: progress(stage, (base + count) / total)))
    prepared += manifest[kind]['size']
  progress('transferring', 0.)
  for index, kind in enumerate(KINDS):
    path = paths[index]
    offset = 0
    with path.open('rb') as source:
      while data := source.read(BLOCK):
        result = request(CHUNK, HEADER.pack(index, offset) + data, raw=True)
        offset += len(data)
        sent += len(data)
        if result.get('index') != index or result.get('offset') != offset:
          raise ValueError('USB acknowledgement offset mismatch')
        progress('transferring', sent / total)
    if offset != manifest[kind]['size']:
      raise ValueError('Cached USB asset changed during transfer: ' + NAMES[index])
  progress('verifying', 1.)
  if request(FINISH, {}, timeout=120).get('complete') is not True:
    raise ValueError('USB update registration incomplete')
