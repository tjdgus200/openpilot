import dataclasses
import hashlib
import threading
import time
from types import SimpleNamespace as NS

import pytest

from openpilot.system.pv5_incidents import core
from openpilot.system.pv5_incidents.memory_pressure import FIXED_FIELDS, MemoryPressure, validate_memory
from openpilot.system.pv5_incidents.memory_sender import MemorySender, Transfer


def envelope():
  return {'format': 2, 'id': 'a' * 32, 'captured_at': 1000,
          'fact': {**FIXED_FIELDS, 'severity': 1, 'source_commit': 'b' * 40, 'usage_percent': 85,
                   'pss': [{'name': 'other', 'bytes': 5678, 'age_ms': 600}]}}


@pytest.mark.parametrize(('field', 'bad'), [
  ('format', 2.0), ('id', '../private'), ('captured_at', True), ('vehicle', 'other'), ('kind', 'system_alert'),
  ('process', 'ui'), ('code', 'lowMemory'), ('severity', True), ('source_commit', 'key'), ('usage_percent', 84),
  ('usage_percent', 101), ('usage_percent', 85.5), ('pss', []), ('pss', [None] * 17),
  ('age_ms', 120001), ('age_ms', -1), ('bytes', True), ('bytes', 0), ('bytes', (1 << 40) + 1), ('name', '../secret'),
])
def test_strict_schema(field, bad):
  payload = envelope()
  location = payload if field in payload else payload['fact'] if field in payload['fact'] else payload['fact']['pss'][0]
  location[field] = bad
  with pytest.raises(ValueError):
    validate_memory(core.encode(payload))


def test_raw_fields_and_duplicate_json_keys_rejected_at_every_level():
  payload = envelope()
  assert validate_memory(core.encode(payload)) == payload
  for location in (payload, payload['fact'], payload['fact']['pss'][0]):
    location['pid'] = 333
    with pytest.raises(ValueError):
      validate_memory(core.encode(payload))
    del location['pid']
  with pytest.raises(ValueError):
    validate_memory(core.encode(payload).replace(b'"name":', b'"name":"unsafe","name":'))


class SM(dict):
  def __init__(self, now, **topics):
    super().__init__(topics)
    self.logMonoTime = dict.fromkeys(self, int(now * 1e9))
    self.valid, self.alive = dict.fromkeys(self, True), dict.fromkeys(self, True)
    self.reads = 0

  def update(self, timeout):
    self.reads += 1


def topics(now):
  stamp = int(now * 1e9)
  procs = [NS(pid=i, name='private', memPss=(i + 1) * 4096, memPssMonoTime=stamp) for i in range(20)]
  procs.extend([NS(pid=101, name='ui', memPss=1 << 39, memPssMonoTime=stamp - 120_000_000_001),
                NS(pid=102, name='ui', memPss=1 << 39, memPssMonoTime=stamp + 1)])
  manager = NS(processes=[NS(pid=19, name='modeld', running=True)])
  return SM(now, deviceState=NS(memoryUsagePercent=85)), SM(now, procLog=NS(procs=procs), managerState=manager)


def parent(now, factory=lambda: None):
  return NS(proof=core.SpeedProof(now, True, True, True, False, 0., 0., True), source='unknown',
            generation=1, stop=threading.Event(), enabled=lambda: True, factory=factory, key=b'x' * 32)


def test_threshold_interval_deduplication_and_private_top_16(monkeypatch):
  now = 300.
  monkeypatch.setattr(time, 'monotonic', lambda: now)
  reporter = parent(now)
  sender = MemorySender(reporter)
  device, processes = topics(now)
  device['deviceState'].memoryUsagePercent = 84
  sender.collect(device, processes, now)
  assert not sender.queue and processes.reads == 0
  device['deviceState'].memoryUsagePercent = 85
  reporter.proof = dataclasses.replace(reporter.proof, pv5=False)
  sender.collect(device, processes, now)
  assert not sender.queue and processes.reads == 0
  reporter.proof = dataclasses.replace(reporter.proof, pv5=True, raw_speed=5., standstill=False)
  sender.collect(device, processes, now)
  assert len(sender.queue) == 1
  samples = validate_memory(sender.queue[0][1])['fact']['pss']
  assert len(samples) == 16 and samples[0] == {'name': 'modeld', 'bytes': 20 * 4096, 'age_ms': 0}
  assert samples[1]['name'] == 'other' and samples[-1]['bytes'] == 5 * 4096
  sender.pump(now)
  assert sender.transfer is None
  now += 299.99
  device, newer = topics(now)
  sender.collect(device, newer, now)
  assert len(sender.queue) == 1 and newer.reads == 0
  now += .01
  device, newer = topics(now)
  sender.collect(device, newer, now)
  assert len(sender.queue) == 2
  assert sender.capture.observe(newer, now, 'unknown', device) is None


def test_stale_or_empty_pss_does_not_start_cooldown():
  capture = MemoryPressure()
  device, sm = topics(300.)
  assert capture.observe(sm, 306., 'unknown', device) is None
  sm['procLog'].procs = []
  assert capture.observe(sm, 300., 'unknown', device) is None
  assert capture.next_capture == 0
  sm.logMonoTime['procLog'] += 1_000_000_000
  sm['procLog'].procs = topics(301.)[1]['procLog'].procs
  assert capture.observe(sm, 301., 'unknown', device)


class Connection:
  def __init__(self, hook=lambda: None):
    self.hook = hook
    self.body = None
    self.closed = False
    self.status = 200
    self.expected = None

  def connect(self):
    self.hook()

  def request(self, method, path, body, headers):
    assert self.auto_open == 0 and (method, path) == ('POST', '/v1/incident')
    assert core.authenticated(b'x' * 32, int(headers['X-PV5-Time']), headers['X-PV5-Nonce'],
                              headers['X-PV5-Signature'], body, time.time())
    self.body = body

  def getresponse(self):
    return self

  def read(self, size):
    return core.encode({'accepted': self.expected or hashlib.sha256(self.body).hexdigest()})

  def close(self):
    self.closed = True


@pytest.mark.parametrize('change', ['motion', 'stale', 'moved_then_stopped', 'stop', 'disabled'])
def test_tls_recheck_never_transmits_with_revoked_permission(monkeypatch, change):
  now = 100.
  monkeypatch.setattr(time, 'monotonic', lambda: now)
  conn = Connection()
  reporter = parent(now, lambda: conn)
  def revoke():
    if change == 'motion':
      reporter.proof = dataclasses.replace(reporter.proof, raw_speed=1.)
    elif change == 'stale':
      reporter.proof = dataclasses.replace(reporter.proof, stamp=now - .25)
    elif change == 'moved_then_stopped':
      reporter.generation += 1
    elif change == 'disabled':
      reporter.enabled = lambda: False
    else:
      reporter.stop.set()
  conn.hook = revoke
  transfer = Transfer(reporter, core.encode(envelope()))
  transfer.run()
  assert conn.closed and conn.body is None and not transfer.acknowledged


def test_inflight_revocation_shuts_down_private_socket_and_keeps_snapshot():
  transfer_entered, socket_closed = threading.Event(), threading.Event()
  conn = Connection()
  conn.sock = NS(shutdown=lambda _: socket_closed.set())
  def block_tls():
    transfer_entered.set()
    assert socket_closed.wait(2.)
  conn.hook = block_tls
  now = time.monotonic()
  reporter = parent(now, lambda: conn)
  sender = MemorySender(reporter)
  sender.queue.append((time.time(), core.encode(envelope())))
  sender.pump(now)
  assert transfer_entered.wait(2.)
  reporter.proof = dataclasses.replace(reporter.proof, standstill=False)
  sender.pump(time.monotonic())
  sender.transfer.join(2.)
  assert socket_closed.is_set() and conn.closed and conn.body is None
  sender.pump(time.monotonic())
  assert sender.transfer is None and len(sender.queue) == 1 and sender.retry.failures == 0


def test_wrong_ack_retries_same_signed_envelope_then_removes_only_after_ack(monkeypatch):
  now = 100.
  monkeypatch.setattr(time, 'monotonic', lambda: now)
  conn = Connection()
  conn.expected = 'bad'
  reporter = parent(now, lambda: conn)
  sender = MemorySender(reporter)
  data = core.encode(envelope())
  sender.queue.append((time.time(), data))
  sender.pump(now)
  sender.transfer.join(2.)
  sender.pump(now)
  assert conn.body == data and conn.closed and len(sender.queue) == 1
  assert sender.transfer is None and sender.retry.next_at == now + 2
  now += 2
  conn = Connection()
  reporter.proof = dataclasses.replace(reporter.proof, stamp=now)
  sender.pump(now)
  sender.transfer.join(2.)
  sender.pump(now)
  assert conn.body == data and conn.closed and not sender.queue and sender.retry.failures == 0
