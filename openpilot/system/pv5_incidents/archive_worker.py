"""Admitted idle-priority copy/filter worker. No video decoding/re-encoding."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import resource
import select
import signal
import ssl
import subprocess
import sys
import threading
import time

from openpilot.system.pv5_incidents.archive_catalog import Catalog, SourceChanged
from openpilot.system.pv5_incidents.archive_protocol import RLOG_NAMES
from openpilot.system.pv5_incidents.archive_protocol import BATCH, CHUNK, TOKEN, advance, encode, headers
from openpilot.system.pv5_incidents.archive_sanitize import PrivacyCatalog, Sanitizer

CONFIG = Path('/data/pv5-pc-archive.json')
MODULES = (
  'archive_worker.py',
  'archive_catalog.py',
  'archive_gate.py',
  'archive_protocol.py',
  'archive_runtime.py',
  'archive_stream.py',
  'archive_sanitize.py',
  'archive_parallel.py',
  'archive_offload.py',
)


class Paused(RuntimeError):
  pass


def module_identity():
  digest = hashlib.sha256()
  for name in MODULES:
    digest.update(name.encode() + b'\0' + (Path(__file__).parent / name).read_bytes().replace(b'\r\n', b'\n'))
  return digest.hexdigest()


def configuration(path=CONFIG):
  info = path.lstat()
  if path.is_symlink() or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o077 or not 0 < info.st_size <= 8192:
    raise ValueError('private_archive_config_required')
  value = json.loads(path.read_bytes())
  if (
    set(value) - {'pc_preprocess', 'qualification_expires_at'} != {'version', 'vehicle', 'enabled', 'qualified_modes', 'wifi_profiles', 'module_identity'}
    or value['version'] != 1
    or value['vehicle'] != 'KIA_PV5'
    or value['enabled'] is not True
    or value['module_identity'] != module_identity()
  ):
    raise ValueError('unqualified_archive_config')
  modes, profiles = value['qualified_modes'], value['wifi_profiles']
  if (
    not isinstance(modes, list)
    or not modes
    or len(modes) > 2
    or len(set(modes)) != len(modes)
    or any(mode not in ('detached_offroad', 'dashcam_stationary') for mode in modes)
    or not isinstance(profiles, list)
    or not 1 <= len(profiles) <= 8
    or any(not isinstance(token, str) or not TOKEN.fullmatch(token) for token in profiles)
  ):
    raise ValueError('invalid_archive_admission')
  if type(value.get('pc_preprocess', False)) is not bool or (value.get('pc_preprocess') and modes != ['detached_offroad']):
    raise ValueError('pc_preprocess_home_only')
  if 'qualification_expires_at' in value:
    expires = value['qualification_expires_at']
    if (value.get('pc_preprocess') is not True or modes != ['detached_offroad']
        or type(expires) is not int or not time.time() < expires <= time.time() + 600):
      raise ValueError('invalid_home_qualification_window')
  return value


def wifi_profile():
  # UUIDs are kept private and hashed. An unregistered hotspot cannot authorize
  # bulk transfer merely by being reported as unmetered Wi-Fi.
  result = subprocess.run(
    ['/usr/bin/nmcli', '-t', '-f', 'UUID,TYPE', 'connection', 'show', '--active'], stdin=subprocess.DEVNULL, capture_output=True, timeout=1, check=True
  )
  if len(result.stdout) > 8192:
    raise Paused('network_profile')
  found = []
  for line in result.stdout.decode('ascii').splitlines():
    token, separator, kind = line.partition(':')
    if separator and kind in ('802-11-wireless', 'wifi') and re.fullmatch(r'[0-9a-fA-F-]{36}', token):
      found.append(hashlib.sha256(token.lower().encode()).hexdigest())
  if len(found) != 1:
    raise Paused('network_profile')
  return found[0]


class Permission:
  def __init__(self, fd):
    if os.getpgrp() != os.getpid():
      raise ValueError('dedicated_worker_session_required')
    self.fd, self.stamp, self.closed = fd, None, False
    self.lock = threading.Lock()
    threading.Thread(target=self.watch, name='archive-permission', daemon=True).start()

  def allowed(self):
    with self.lock:
      return not self.closed and self.stamp is not None and 0 <= time.monotonic() - self.stamp < 0.4

  def watch(self):
    started = time.monotonic()
    buffer = b''
    while True:
      readable, _, _ = select.select([self.fd], [], [], 0.05)
      if readable:
        data = os.read(self.fd, 4096)
        if not data:
          break
        buffer += data
        if len(buffer) > 4096:
          break
        while b'\n' in buffer:
          raw, buffer = buffer.split(b'\n', 1)
          try:
            stamp = float(raw)
          except ValueError:
            stamp = -1.0
          if not 0 <= time.monotonic() - stamp < 0.4:
            self.closed = True
            break
          with self.lock:
            self.stamp = stamp
      if self.closed or (time.monotonic() - started > 1 and not self.allowed()):
        break
    # This process is the leader of the session created specifically for it.
    # Kill only this copier and its own short-lived nmcli children.
    os.killpg(os.getpid(), signal.SIGKILL)


class Transport:
  def __init__(self, target, key, allowed, timeout=6):
    if target.scheme != 'https' or not target.hostname.endswith('.ts.net') or target.port != 10000 or target.path != '/v1/incident':
      raise ValueError('unapproved_archive_origin')
    self.target, self.key, self.allowed, self.timeout = target, key, allowed, timeout
    self.connection = None
    self.before_send = lambda: True

  def request(self, path, body):
    if self.allowed() is not True:
      raise Paused('state')
    if self.connection is None:
      self.connection = http.client.HTTPSConnection(self.target.hostname, self.target.port, timeout=self.timeout, context=ssl.create_default_context())
      self.connection.connect()
    if self.allowed() is not True:
      self.close()
      raise Paused('state')
    if self.before_send() is not True:
      raise Paused('network_profile')
    self.connection.request('POST', path, body=body, headers=headers(self.key, path, body))
    response = self.connection.getresponse()
    data = response.read(8193)
    if self.allowed() is not True:
      self.close()
      raise Paused('state')
    if len(data) > 8192:
      raise Paused('network')
    if response.status == 507:
      raise Paused('storage')
    if response.status != 200:
      raise Paused('network')
    return json.loads(data)

  def close(self):
    if self.connection is not None:
      self.connection.close()
      self.connection = None


class Copier:
  def __init__(self, catalog, transport, allowed, profiles, profile=wifi_profile, rate=256 * 1024, clock=time.monotonic, sleep=time.sleep, cpu=None,
               home_unlimited=False, pacer=None):
    self.catalog, self.transport, self.allowed, self.profiles, self.profile = catalog, transport, allowed, profiles, profile
    self.rate, self.clock, self.sleep = rate, clock, sleep
    self.home_unlimited = home_unlimited is True
    self.pacer = pacer
    self.cpu = cpu or (
      lambda: sum((resource.getrusage(kind).ru_utime + resource.getrusage(kind).ru_stime) for kind in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN))
    )
    self.start, self.cpu_start = clock(), self.cpu()
    self.bytes, self.segments = 0, 0
    self.transport.before_send = self.profile_check

  def check(self):
    if self.allowed() is not True:
      raise Paused('state')
    if self.clock() - self.start >= 300 or (not self.home_unlimited and self.cpu() - self.cpu_start >= 10.0):
      raise Paused('budget')
    if self.home_unlimited:
      return True
    # At most one CPU-second per 20 wall-seconds, measured across this worker
    # and reaped helpers. Device qualification must still measure actual peaks.
    while self.cpu() - self.cpu_start > 0.05 + (self.clock() - self.start) / 20:
      if self.allowed() is not True:
        raise Paused('state')
      if self.clock() - self.start >= 300:
        raise Paused('budget')
      self.sleep(0.05)
    return True

  def profile_check(self):
    self.check()
    if self.profile() not in self.profiles:
      raise Paused('network_profile')
    self.check()
    return True

  def run(self, commit):
    try:
      self.profile_check()
      while self.check():
        candidate = self.catalog.next(commit)
        if candidate is None:
          return {'reason': 'idle', 'bytes': self.bytes, 'segments': self.segments}
        value = candidate['manifest']
        self.check()
        begin = self.transport.request('/v1/archive/manifest', encode(value))
        states = {entry['file_id']: entry for entry in begin['files']}
        batched = self.home_unlimited and type(begin.get('max_batch_bytes')) is int and begin['max_batch_bytes'] == BATCH
        for entry in value['files']:
          state = states[entry['file_id']]
          offset, chain = state['offset'], state['chain']
          if type(offset) is not int or not 0 <= offset <= entry['size'] or not TOKEN.fullmatch(chain):
            raise Paused('network')
          if state['complete']:
            if offset != entry['size']:
              raise Paused('network')
            continue
          while offset < entry['size']:
            self.check()
            # Keep the original source/privacy/admission check before and after
            # every 64-KiB read, even when sending one negotiated home batch.
            maximum = BATCH if batched else CHUNK
            pieces = []
            for start in range(offset, min(entry['size'], offset + maximum), CHUNK):
              self.check()
              pieces.append(self.catalog.read(candidate, entry, start, CHUNK))
            body = b''.join(pieces)
            self.check()
            endpoint = 'batch' if batched else 'chunk'
            result = self.transport.request(f"/v1/archive/{endpoint}/{entry['file_id']}/{offset}", body)
            expected = chain
            for piece in pieces:
              expected = advance(expected, piece)
            if result.get('offset') != offset + len(body) or result.get('chain') != expected:
              raise Paused('network')
            chain, offset = expected, offset + len(body)
            self.bytes += len(body)
            if self.pacer is not None:
              self.pacer(len(body), self.check)
            while self.pacer is None and self.clock() - self.start < self.bytes / self.rate:
              self.check()
              self.sleep(0.05)
          self.check()
          result = self.transport.request('/v1/archive/seal/' + entry['file_id'], encode({'size': entry['size'], 'chain': chain}))
          if result.get('complete') is not True:
            raise Paused('network')
        self.check()
        self.catalog.completed(candidate)
        self.segments += 1
    finally:
      self.transport.close()


def share_home_burst(copier, start, transferred, completed):
  # Preserve the original overall deadline and pacing when switching phases.
  copier.start = start
  copier.bytes, copier.segments = transferred, completed


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', type=Path, required=True)
  parser.add_argument('--log-root', type=Path, required=True)
  parser.add_argument('--catalog', type=Path, required=True)
  parser.add_argument('--commit', required=True)
  args = parser.parse_args()
  reason, copier = 'unqualified', None
  try:
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    cfg = configuration(args.config)
    home_unlimited = cfg['qualified_modes'] == ['detached_offroad']
    if not home_unlimited:
      available = os.sched_getaffinity(0) & set(range(4))
      if not available:
        raise Paused('resource')
      os.sched_setaffinity(0, available)
      resource.setrlimit(resource.RLIMIT_AS, (192 * 1024**2, 192 * 1024**2))
      resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    permission = Permission(sys.stdin.fileno())
    began = time.monotonic()
    while not permission.allowed() and time.monotonic() - began < 0.9:
      time.sleep(0.02)
    if not permission.allowed():
      raise Paused('state')
    from openpilot.system.pv5_incidents.runtime import read_config

    target, key = read_config()
    # Held only by this upload worker, never a logger/deleter lock.
    import fcntl

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    worker_lock = args.catalog.with_suffix('.lock').open('a+b')
    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Separate durable log-first cursor. Full manifests still include every road
    # file; identical filtered log identities let the PC skip retransmission.
    catalogs = []
    if home_unlimited:
      catalogs.append(Catalog(args.log_root, args.catalog.with_name('archive-home-logs.sqlite'), key, permission.allowed, groups=(RLOG_NAMES,)))
    catalogs.append(Catalog(args.log_root, args.catalog, key, permission.allowed))
    if cfg.get('pc_preprocess') is True:
      from openpilot.system.pv5_incidents.archive_offload import upload
      result = upload(catalogs, target, key, permission.allowed, cfg['wifi_profiles'], args.commit)
      print(json.dumps(result), flush=True)
      return
    total_bytes, total_segments = 0, 0
    burst_started = time.monotonic()
    for raw in catalogs:
      transport = Transport(target, key, permission.allowed, timeout=30 if home_unlimited else 6)
      copier = Copier(raw, transport, permission.allowed, cfg['wifi_profiles'],
                      rate=8 * 1024**2 if home_unlimited else 256 * 1024, home_unlimited=home_unlimited)
      if home_unlimited:
        share_home_burst(copier, burst_started, total_bytes, total_segments)
      copier.catalog = PrivacyCatalog(raw, Sanitizer(args.catalog.parent / 'privacy-cache', copier.check,
                                                    detached_home_fast=home_unlimited))
      result = copier.run(args.commit)
      total_bytes, total_segments = result['bytes'], result['segments']
    copier.bytes, copier.segments = total_bytes, total_segments
    reason = result['reason']
  except Paused as error:
    reason = str(error) if str(error) in ('state', 'budget', 'network', 'network_profile', 'storage', 'resource') else 'paused'
  except SourceChanged:
    reason = 'source'
  except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
    reason = 'worker_error'
  print(json.dumps({'reason': reason, 'bytes': copier.bytes if copier else 0, 'segments': copier.segments if copier else 0}), flush=True)


if __name__ == '__main__':
  main()
