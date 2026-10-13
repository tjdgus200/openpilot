"""Comma downloads a pinned personal release, then relays verified bytes at boot."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import urllib.request

from tools.jetlink import relay_manifest as wire  # noqa: TID251 -- shared standalone boot-updater protocol

CACHE = Path('/data/jetson-comma-relay')
PUBLIC_KEY = Path(__file__).with_name('comma-relay-public.pem')


def offroad():
  try:
    return (Path('/data/params/d/IsOffroad').read_bytes() == b'1' and
            Path('/data/params/d/IsOnroad').read_bytes() == b'0')
  except OSError:
    return False


def digest(path):
  value = hashlib.sha256()
  with path.open('rb') as stream:
    for block in iter(lambda: stream.read(4 << 20), b''):
      value.update(block)
  return value.hexdigest()


class Redirects(urllib.request.HTTPRedirectHandler):
  def redirect_request(self, req, fp, code, msg, headers, newurl):
    wire.checked_url(newurl, redirect=True)
    return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(item, path, cancelled):
  if path.is_symlink():
    raise ValueError('Invalid comma relay cache symlink')
  if path.is_file() and path.stat().st_size == item['size'] and digest(path) == item['sha256']:
    return
  temporary = path.with_suffix('.part')
  try:
    opener = urllib.request.build_opener(Redirects())
    with opener.open(wire.checked_url(item['url']), timeout=30) as source, temporary.open('wb') as output:
      wire.checked_url(source.url, redirect=True)
      count = 0
      for block in iter(lambda: source.read(1 << 20), b''):
        if cancelled():
          raise InterruptedError('Personal update requires offroad and a live connection')
        count += len(block)
        if count > item['size']:
          raise ValueError('Oversized relay download')
        output.write(block)
      output.flush()
      os.fsync(output.fileno())
    if count != item['size'] or digest(temporary) != item['sha256']:
      raise ValueError('Relay download checksum mismatch')
    os.replace(temporary, path)
  finally:
    temporary.unlink(missing_ok=True)


class Sender:
  def __init__(self, manifest, connected, parked=offroad):
    wire.verify(manifest, PUBLIC_KEY.read_text())
    self.manifest, self.connected, self.parked = manifest, connected, parked
    self.id = wire.identity(manifest)
    self.path = CACHE / self.id
    self.stopped = threading.Event()
    self.worker = None
    self.error = None
    self.offsets = None
    self.complete = False

  def cancelled(self):
    return self.stopped.is_set() or not self.connected() or not self.parked()

  def download(self):
    try:
      self.path.mkdir(parents=True, exist_ok=True)
      # flock prevents concurrent workers from sharing partial files after a
      # disconnect. A previous network read may take up to 30 seconds to stop.
      import fcntl
      with (self.path / 'download.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(self.path).free < sum(self.manifest[n]['size'] for n in wire.ASSETS) + (256 << 20):
          raise OSError('Insufficient comma DATA space for personal update')
        for name in wire.ASSETS:
          if self.cancelled():
            raise InterruptedError('Personal update cancelled')
          fetch(self.manifest[name], self.path / name, self.cancelled)
    except Exception as exc:
      self.error = exc

  def request(self, client, kind, payload, timeout=10):
    if self.cancelled():
      raise InterruptedError('Personal relay requires offroad')
    seq = client._next_seq()
    client.t.send(kind, seq, (payload,), timeout=timeout)
    result = json.loads(bytes(client._expect(wire.ACK, seq, timeout).payload))
    if (result.get('id') != self.id or type(result.get('complete')) is not bool or
        not isinstance(result.get('offsets'), list) or len(result['offsets']) != 2 or
        any(type(n) is not int or not 0 <= n <= self.manifest[name]['size']
            for n, name in zip(result['offsets'], wire.ASSETS, strict=True))):
      raise ValueError('Invalid relay acknowledgement')
    return result

  def pump(self, client):
    if not self.parked():
      self.stopped.set()
      raise InterruptedError('Personal update waits until offroad; no activation requested')
    if self.complete:
      return 'verifying'
    if self.worker is None:
      self.worker = threading.Thread(target=self.download, daemon=True)
      self.worker.start()
    if self.worker.is_alive():
      return 'comma_downloading'
    if self.error is not None:
      raise self.error
    if self.offsets is None:
      self.offsets = self.request(client, wire.QUERY, bytes.fromhex(self.id))['offsets']
    start = time.monotonic()
    for index, name in enumerate(wire.ASSETS):
      with (self.path / name).open('rb') as source:
        source.seek(self.offsets[index])
        while self.offsets[index] < self.manifest[name]['size']:
          block = source.read(min(wire.CHUNK_BYTES, self.manifest[name]['size'] - self.offsets[index]))
          if not block:
            raise ValueError('Comma relay cache truncated')
          expected = self.offsets.copy()
          expected[index] += len(block)
          payload = wire.HEADER.pack(bytes.fromhex(self.id), index, self.offsets[index]) + block
          answer = self.request(client, wire.CHUNK, payload)
          if answer['offsets'] != expected:
            raise ValueError('Unexpected relay offset')
          self.offsets = answer['offsets']
          if time.monotonic() - start > .25:
            return 'comma_uploading'
    answer = self.request(client, wire.FINISH, bytes.fromhex(self.id), timeout=120)
    if not answer['complete']:
      raise ValueError('Relay verification did not finish')
    self.complete = True
    return 'verifying'

  def close(self):
    self.stopped.set()
