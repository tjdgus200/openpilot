"""Comma-side signed asset download and stop-and-wait boot-gate USB upload."""
import hashlib
import json
import os
from pathlib import Path
import urllib.request

from tools.jetlink.usb_release import ACK, BEGIN, BLOCK, CHUNK, FINISH, HEADER, KINDS, NAMES, verify  # noqa: TID251


def asset(item, cache, allowed):
  cache.mkdir(parents=True, exist_ok=True)
  path = cache / item['sha256']
  def matches():
    if not path.is_file() or path.stat().st_size != item['size']:
      return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
      while data := stream.read(1 << 20):
        if not allowed():
          raise ConnectionError('USB update requires offroad and connected state')
        digest.update(data)
    return digest.hexdigest() == item['sha256']
  if matches():
    return path
  partial = path.with_suffix('.part')
  try:
    if not allowed():
      raise ConnectionError('USB update requires offroad and connected state')
    with urllib.request.urlopen(item['url'], timeout=30) as source, partial.open('wb') as output:
      if not source.url.startswith('https://'):
        raise ValueError('Insecure asset redirect')
      count, digest = 0, hashlib.sha256()
      while data := source.read(BLOCK):
        if not allowed():
          raise ConnectionError('USB update interrupted by onroad/disconnect')
        count += len(data)
        if count > item['size']:
          raise ValueError('Oversized USB update asset')
        digest.update(data)
        output.write(data)
      output.flush()
      os.fsync(output.fileno())
    if count != item['size'] or digest.hexdigest() != item['sha256']:
      raise ValueError('USB update download hash mismatch')
    os.replace(partial, path)
    return path
  finally:
    partial.unlink(missing_ok=True)


def upload(client, manifest, key, cache, allowed, progress):
  verify(manifest, key)
  def exchange(kind, payload, raw=False, timeout=30):
    if not allowed():
      raise ConnectionError('USB update requires offroad and connected state')
    seq = client._next_seq()
    if raw:
      client.t.send(kind, seq, [payload], timeout=timeout)
    else:
      client.t.send(kind, seq, [json.dumps(payload).encode()], timeout=timeout)
    response = client.t.recv(timeout=timeout)
    if response.msg_type != ACK or response.seq != seq or len(response.payload) > 65536:
      raise ValueError('Unexpected USB update acknowledgement')
    value = json.loads(bytes(response.payload))
    if not isinstance(value, dict):
      raise ValueError('Invalid USB update acknowledgement')
    if value.get('ok') is not True:
      raise ValueError('Jetson rejected USB update: ' + str(value.get('error', 'invalid reply')))
    return value
  # An already registered package needs no network. BEGIN cannot activate it.
  result = exchange(BEGIN, manifest, timeout=120)
  if result.get('complete') is True:
    return
  total, sent = sum(manifest[k]['size'] for k in KINDS), 0
  for index, kind in enumerate(KINDS):
    progress('downloading', sent / total)
    path = asset(manifest[kind], Path(cache), allowed)
    offset = 0
    with path.open('rb') as source:
      while data := source.read(BLOCK):
        result = exchange(CHUNK, HEADER.pack(index, offset) + data, raw=True)
        offset += len(data)
        sent += len(data)
        if result.get('index') != index or result.get('offset') != offset:
          raise ValueError('USB acknowledgement offset mismatch')
        progress('transferring', sent / total)
    if offset != manifest[kind]['size']:
      raise ValueError('Cached USB asset changed during transfer: ' + NAMES[index])
  progress('verifying', 1.)
  if exchange(FINISH, {}, timeout=120).get('complete') is not True:
    raise ValueError('USB update registration incomplete')
