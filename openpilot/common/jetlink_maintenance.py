"""Exclusive native-model ownership for maintenance of an unused Jetson.

An acknowledged hold survives a requester crash. Only an explicit release may
allow joining again; a dead updater must never leave an installation racing a
new external inference request. No camera tensors or control flags are changed.
"""
import json
import math
import os
from pathlib import Path
import secrets
import time

CAPABILITY = 'carrot_internal_update_v1'
REQUEST = Path('/dev/shm/carrot-jetlink-maintenance-request.json')
ACK = Path('/dev/shm/carrot-jetlink-maintenance-ack.json')


def read(path):
  try:
    with path.open('rb') as source:
      value = json.loads(source.read(4097))
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


def write(path, value):
  temporary = path.with_suffix(f'.{os.getpid()}.tmp')
  temporary.write_text(json.dumps(value))
  os.replace(temporary, path)


def fresh(value, now, limit):
  stamp = value.get('updated')
  return type(stamp) in (float, int) and math.isfinite(stamp) and 0 <= now - stamp < limit


def identity():
  from openpilot.common.big_model_catalog import boot_id, effective_id, usb_release
  try:
    value = usb_release(effective_id())
  except (OSError, ValueError, KeyError, TypeError):
    value = None
  return {'boot': boot_id(), 'release': value['source_commit'] if value else ''}


class Owner:
  """Called by modeld, never by the transport daemon."""
  def __init__(self, clock=time.monotonic):
    self.clock, self.identity = clock, identity()
    self.token = None
    self.next_poll = self.next_ack = 0.

  def held(self, *, external):
    now = self.clock()
    if now >= self.next_poll:
      self.next_poll = now + .2
      request = read(REQUEST)
      token = request.get('token')
      if isinstance(token, str) and len(token) == 32:
        if request.get('action') == 'release' and token == self.token:
          self.token = None
        elif request.get('action') == 'hold' and not external:
          # /dev/shm is boot-local. Retain a hold across a modeld restart too;
          # only fresh completed outputs may approve the current updater.
          self.token = token
    return self.token is not None

  def completed(self):
    # A proof is emitted only AFTER a successful native-model output. Checking
    # transport disconnection, UsbGpuActive or an old status JSON is insufficient.
    now = self.clock()
    if self.token is not None and now >= self.next_ack:
      try:
        write(ACK, dict(self.identity, token=self.token, updated=now, internal=True))
      except OSError:
        pass  # Unavailable proof denies maintenance, not local inference.
      self.next_ack = now + .1


class Lease:
  def __init__(self, clock=time.monotonic):
    self.clock, self.token, self.next_write = clock, secrets.token_hex(16), 0.
    self.proof = {}

  def allowed(self):
    now = self.clock()
    try:
      if now >= self.next_write:
        write(REQUEST, {'action': 'hold', 'token': self.token, 'updated': now})
        self.next_write = now + .2
      value = read(ACK)
      self.proof = value if (value.get('token') == self.token and value.get('internal') is True
                             and fresh(value, now, .25)) else {}
      return bool(self.proof)
    except OSError:
      self.proof = {}
      return False

  def release(self):
    write(REQUEST, {'action': 'release', 'token': self.token, 'updated': self.clock()})
    self.token, self.proof, self.next_write = secrets.token_hex(16), {}, 0.


_lease = None


def lease():
  global _lease
  if _lease is None:
    _lease = Lease()
    saved = read(REQUEST)
    if saved.get('action') == 'hold' and isinstance(saved.get('token'), str) and len(saved['token']) == 32:
      _lease.token = saved['token']  # Recover/release modeld's hold after daemon restart.
  return _lease
