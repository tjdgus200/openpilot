"""Opt-in diagnostic threads inside tombstoned, not modeld/controlsd/manager."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import ssl
import stat
import threading
import time
from urllib.parse import urlsplit

from openpilot.system.pv5_incidents.core import (MAX_BODY, MAX_SPOOL, Pending, Retry, SpeedProof,
                                               encode, signature, stopped)

CONFIG = Path('/data/pv5-incident-report.json')
SPOOL = Path('/data/pv5-incidents/pending.json')
_reporter = None


def read_config(path=CONFIG):
  info = path.lstat()
  if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0, os.getuid()) or info.st_size > 4096:
    raise ValueError('private_config_required')
  with path.open('rb') as source:
    value = json.loads(source.read(4097))
  if set(value) != {'endpoint', 'key'}:
    raise ValueError('invalid_config')
  target = urlsplit(value['endpoint'])
  if (target.scheme != 'https' or not target.hostname or not target.hostname.endswith('.ts.net') or
      target.username or target.password or target.port not in (None, 443, 8443, 10000) or
      target.path != '/v1/incident' or target.query or target.fragment):
    raise ValueError('invalid_endpoint')
  key = bytes.fromhex(value['key'])
  if len(key) != 32:
    raise ValueError('invalid_key')
  return target, key


def speed_proof(sm):
  cs, cp = sm['carState'], sm['carParams']
  return SpeedProof(sm.logMonoTime['carState'] / 1e9,
                    bool(sm.valid['carState'] and sm.alive['carState']),
                    bool(sm.seen['carParams'] and sm.valid['carParams'] and not cp.notCar and cp.carFingerprint == 'KIA_PV5'),
                    bool(cs.canValid), bool(cs.canTimeout), cs.vEgoRaw, cs.vEgo, bool(cs.standstill))


class Reporter:
  def __init__(self, sm, enabled, target, key, spool=SPOOL, connection=None):
    self.sm, self.enabled, self.target, self.key, self.spool = sm, enabled, target, key, spool
    self.queue, self.retry = Pending(), Retry()
    self.lock = threading.Lock()
    self.stop = threading.Event()
    self.wake = threading.Event()
    self.proof = None
    self.connection = None
    self.generation = 0
    self.previous = set()
    self.next_checkpoint = 0.
    self.last_saved = None
    self.source = os.environ.get('GIT_COMMIT', '')
    if len(self.source) != 40 or any(c not in '0123456789abcdef' for c in self.source):
      self.source = 'unknown'
    self.factory = connection or (lambda: http.client.HTTPSConnection(
      target.hostname, target.port or 443, timeout=2., context=ssl.create_default_context()))

  def add(self, fact, now=None):
    if self.proof is None or not self.proof.pv5:
      return
    # Only tiny bounded queue work, never network or disk under this lock.
    with self.lock:
      self.queue.add(fact, time.time() if now is None else now)
    self.wake.set()

  def crash(self, event):
    self.add({'vehicle': 'KIA_PV5', 'kind': 'process_exit', 'process': event.get('name'),
              'code': event.get('exitcode'), 'source_commit': self.source, 'severity': 3})

  def observe(self, sm):
    self.proof = speed_proof(sm)
    if not self.proof.pv5:
      self.previous.clear()
      self.cancel()
      return
    current = set()
    # Typed active events only. No raw log subscription, text parsing, tmux,
    # camera data, inference timers or per-frame I/O are introduced.
    if sm.valid['onroadEvents'] and sm.alive['onroadEvents']:
      for event in list(sm['onroadEvents'])[:32]:
        code = str(event.name)
        permanent_fault = event.permanent and code in {
          'canError', 'cameraMalfunction', 'processNotRunning', 'commIssue', 'modeldLagging', 'overheat', 'steerUnavailable'}
        if not (event.warning or event.noEntry or event.softDisable or event.immediateDisable or permanent_fault):
          continue
        severity = 3 if event.immediateDisable else 2 if event.softDisable else 1
        item = (code, severity)
        current.add(item)
        if item not in self.previous:
          self.add({'vehicle': 'KIA_PV5', 'kind': 'system_alert', 'process': 'selfdrived',
                    'code': code, 'source_commit': self.source, 'severity': severity})
      # Don't manufacture a clear/reappearance from a temporarily stale event feed.
      self.previous = current
    if stopped(self.proof, time.monotonic()):
      self.wake.set()
    else:
      self.cancel()

  def cancel(self):
    self.generation += 1
    connection = self.connection
    if connection is not None:
      try:
        connection.close()
      except OSError:
        pass

  def checkpoint(self, now):
    if now < self.next_checkpoint or not stopped(self.proof, now):
      return
    with self.lock:
      self.queue.expire(time.time())
      data = self.queue.dump()
    self.next_checkpoint = now + 30.
    if data == self.last_saved:
      return
    try:
      self.spool.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
      temporary = self.spool.with_suffix('.tmp')
      descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
      with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
      temporary.replace(self.spool)
      self.last_saved = data
    except OSError:
      pass  # Full/read-only disk leaves the bounded RAM queue and uploader alive.

  def send_once(self, now):
    if not stopped(self.proof, now) or not self.retry.ready(now):
      return False
    with self.lock:
      batch = self.queue.batch(time.time())
    if not batch['rows']:
      return False
    data = encode(batch)
    if len(data) > MAX_BODY:
      return False
    generation = self.generation
    connection = None
    try:
      connection = self.factory()
      self.connection = connection
      connection.connect()
      # Cancellation must not cause http.client to reopen the socket on request().
      connection.auto_open = 0
      # DNS/connect/TLS cannot authorize a later moving/stale car to send data.
      if generation != self.generation or not stopped(self.proof, time.monotonic()) or self.stop.is_set():
        return False
      stamp, nonce = int(time.time()), secrets.token_hex(16)
      headers = {'Content-Type': 'application/json', 'X-PV5-Time': str(stamp), 'X-PV5-Nonce': nonce,
                 'X-PV5-Signature': signature(self.key, stamp, nonce, data)}
      connection.request('POST', '/v1/incident', body=data, headers=headers)
      response = connection.getresponse()
      result = response.read(2049)
      if response.status != 200 or len(result) > 2048 or json.loads(result) != {'accepted': hashlib.sha256(data).hexdigest()}:
        raise OSError('receiver_not_acknowledged')
      with self.lock:
        self.queue.acknowledge(batch)
      self.retry.succeeded(time.monotonic())
      return True
    except (OSError, ValueError, http.client.HTTPException):
      self.retry.failed(time.monotonic())
      return False
    finally:
      if connection is not None:
        connection.close()
      self.connection = None

  def network_loop(self):
    lower_priority()
    while not self.stop.is_set():
      self.wake.wait(.2)
      self.wake.clear()
      if stopped(self.proof, time.monotonic()):
        self.send_once(time.monotonic())

  def run(self):
    lower_priority()
    try:
      if self.spool.is_file() and not self.spool.is_symlink():
        with self.spool.open('rb') as source:
          self.queue.restore(source.read(MAX_SPOOL + 1), time.time())
    except (OSError, ValueError, TypeError, KeyError):
      pass  # Broken local diagnostics must not stop tombstoned.
    thread = threading.Thread(target=self.network_loop, name='pv5-report-upload', daemon=True)
    thread.start()
    try:
      next_option = 0.
      while not self.stop.is_set():
        now = time.monotonic()
        if now >= next_option:
          next_option = now + 1.
          if not self.enabled():
            break
        self.sm.update(0)
        self.observe(self.sm)
        self.checkpoint(now)
        self.stop.wait(.1 if stopped(self.proof, now) else .5)
    except Exception:
      # No recursive logging/error collection if diagnostics itself fails.
      pass
    finally:
      self.stop.set()
      self.cancel()
      self.wake.set()


def lower_priority():
  try:
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
  except (AttributeError, OSError):
    pass


def notify_crash(event):
  try:
    if _reporter is not None:
      _reporter.crash(event)
  except Exception:
    pass


def start_incident_reporting():
  global _reporter
  try:
    from openpilot.common.params import Params
    params = Params()
    if not params.get_bool('Pv5IncidentReporting'):
      return None
    target, key = read_config()
    import openpilot.cereal.messaging as messaging
    sm = messaging.SubMaster(['carParams', 'carState', 'onroadEvents'])
    _reporter = Reporter(sm, lambda: params.get_bool('Pv5IncidentReporting'), target, key)
    threading.Thread(target=_reporter.run, name='pv5-report-observer', daemon=True).start()
    return _reporter
  except Exception:
    return None
