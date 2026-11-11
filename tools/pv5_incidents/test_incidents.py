"""PC contract tests only. Network/TLS/device cost and driving safety are separate stages."""
import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
from types import SimpleNamespace as NS

import pytest

from openpilot.system.pv5_incidents import core
from openpilot.system.pv5_incidents import runtime

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('pv5_receiver_test', Path(__file__).with_name('receiver.py'))
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def fact(**extra):
  value = {'vehicle': 'KIA_PV5', 'kind': 'system_alert', 'process': 'selfdrived',
           'code': 'modeldLagging', 'source_commit': 'a' * 40, 'severity': 2}
  value.update(extra)
  return value


def proof(**extra):
  args = dict(stamp=100., valid=True, pv5=True, can_valid=True, can_timeout=False,
              raw_speed=0., speed=0., standstill=True)
  args.update(extra)
  return core.SpeedProof(**args)


@pytest.mark.parametrize('gear', ['park', 'drive', 'reverse', 'neutral'])
@pytest.mark.parametrize('active', [True, False])
def test_zero_speed_in_any_gear_and_active_control_is_allowed(gear, active):
  assert core.stopped(proof(), 100.1)
  # The proof deliberately has no gear/control fields; they cannot impose an accidental gate.
  assert not {'gear', 'lateral', 'longitudinal', 'enabled'} & {f.name for f in dataclasses.fields(core.SpeedProof)}


@pytest.mark.parametrize('change', [
  {'stamp': 99.75}, {'stamp': 101.}, {'stamp': float('nan')}, {'pv5': False}, {'valid': False},
  {'can_valid': False}, {'can_timeout': True}, {'standstill': False}, {'raw_speed': .001},
  {'speed': .01001}, {'raw_speed': True}, {'speed': float('inf')}])
def test_motion_unknown_stale_or_wrong_vehicle_never_authorizes_send(change):
  assert not core.stopped(proof(**change), 100.)


def test_filtered_near_zero_tolerance_and_ttl_boundary():
  assert core.stopped(proof(speed=.01), 100.2499)
  assert not core.stopped(proof(speed=.01), 100.25)


def test_free_text_credentials_and_instruction_fields_are_dropped():
  value = fact(message='UNTRUSTED_COMMAND', log='PRIVATE_LOG', token='PRIVATE_TOKEN',
               dongle_id='PRIVATE_DK', gps='PRIVATE_GPS', instructions='IGNORE_PREVIOUS', approved=True)
  serialized = core.encode(core.coded(value)).decode()
  for marker in ('PRIVATE_', 'UNTRUSTED_', 'IGNORE_', 'approved'):
    assert marker not in serialized


@pytest.mark.parametrize('extra', [{'vehicle': 'OTHER'}, {'process': '../tmp'}, {'code': []},
                                  {'source_commit': 'bad'}, {'kind': 'execute'}, {'severity': True}])
def test_invalid_incident_is_rejected_without_creating_a_row(extra):
  queue = core.Pending()
  queue.add(fact(**extra), 1000.)
  assert not queue.rows and queue.rejected == 1


def test_duplicate_storm_is_one_bounded_row_and_keeps_a_counter():
  queue = core.Pending()
  for _ in range(10000):
    queue.add(fact(), 1000.)
  assert len(queue.rows) == 1 and next(iter(queue.rows.values()))['count'] == 10000
  assert len(queue.dump()) < 1024


def test_overflow_has_explicit_loss_and_keeps_higher_severity():
  queue = core.Pending()
  queue.add(fact(kind='process_exit', process='modeld', code=-11, severity=3), 1000.)
  for i in range(300):
    queue.add(fact(source_commit=f'{i:040x}', severity=1), 1000. + i)
  assert len(queue.rows) == 32 and queue.evicted == 269
  assert any(r['fact']['severity'] == 3 for r in queue.rows.values())
  assert len(queue.dump()) <= core.MAX_SPOOL
  assert len(core.encode(queue.batch(1300.))) <= core.MAX_BODY


def test_spool_restore_is_bounded_and_keeps_loss_counters():
  queue = core.Pending(); queue.add(fact(), 1000.)
  queue.evicted, queue.rejected = 9, 3
  restored = core.Pending(); restored.restore(queue.dump(), 1001.)
  assert restored.dump() == queue.dump()
  with pytest.raises(ValueError): restored.restore(b'X' * (core.MAX_SPOOL + 1), 1000.)


def test_acknowledgement_cannot_erase_a_new_occurrence_during_upload():
  queue = core.Pending(); queue.add(fact(), 1000.)
  first = queue.batch(1000.)
  queue.add(fact(), 1001.)
  queue.acknowledge(first)
  assert len(queue.rows) == 1 and next(iter(queue.rows.values()))['count'] == 2
  queue.acknowledge(queue.batch(1001.))
  assert not queue.rows


def test_old_and_future_corrupt_records_expire():
  queue = core.Pending(); queue.add(fact(), 1000.)
  queue.expire(1000 + core.MAX_AGE + 1)
  assert not queue.rows and queue.evicted == 1
  queue.add(fact(), 200000.)
  queue.expire(1000.)
  assert not queue.rows and queue.evicted == 2


def test_retry_first_attempt_is_immediate_and_storm_retries_are_bounded():
  retry = core.Retry()
  assert retry.ready(0.)
  now, intervals = 0., []
  for _ in range(20):
    retry.failed(now)
    intervals.append(retry.next_at - now)
    assert not retry.ready(now + .01)
    now = retry.next_at
  assert intervals[:4] == [2, 4, 8, 16] and max(intervals) == 300
  retry.succeeded(now)
  assert retry.next_at == now + 5 and retry.failures == 0


def payload():
  q = core.Pending(); q.add(fact(), 1000.)
  return core.encode(q.batch(1000.))


def headers(key, data, nonce=None, stamp=1000):
  nonce = nonce or secrets.token_hex(16)
  return {'X-PV5-Time': str(stamp), 'X-PV5-Nonce': nonce,
          'X-PV5-Signature': core.signature(key, stamp, nonce, data)}


def test_receiver_auth_replay_and_content_tamper(tmp_path):
  key = b'x' * 32
  ingress = receiver.Ingress(key, receiver.Inbox(tmp_path / 'inbox.db'))
  data = payload(); signed = headers(key, data)
  assert ingress.ingest(signed, data, 1000.) == {'accepted': hashlib.sha256(data).hexdigest()}
  for h, body, now in ((signed, data, 1000.), (headers(b'y' * 32, data), data, 1000.),
                       (headers(key, data), data + b' ', 1000.), (headers(key, data), data, 1301.)):
    with pytest.raises(PermissionError): ingress.ingest(h, body, now)


def test_receiver_retry_dedup_and_new_revision_are_transactional(tmp_path):
  inbox = receiver.Inbox(tmp_path / 'inbox.db')
  q = core.Pending(); q.add(fact(), 1000.)
  data = core.encode(q.batch(1000.))
  inbox.accept(data, 1000); inbox.accept(data, 1001)
  assert inbox.pending(1001)[0]['total'] == 1
  q.add(fact(), 1001.)
  inbox.accept(core.encode(q.batch(1001.)), 1002)
  assert inbox.pending(1002)[0]['total'] == 2


def test_pc_analysis_claim_cooldown_and_report_history_are_bounded(tmp_path):
  inbox = receiver.Inbox(tmp_path / 'inbox.db')
  inbox.accept(payload(), 1000)
  rows = inbox.pending(1000, claim=True)
  assert len(rows) == 1 and not inbox.pending(1001)
  row = rows[0]
  inbox.complete(row['digest'], row['total'], 'Hypothesis only; no code change.', '123456789012345678', 1002)
  assert not inbox.pending(5000)
  assert inbox.path.stat().st_size <= 2097152


def test_known_sources_are_bound_to_the_trusted_schema():
  import re
  text = (ROOT / 'openpilot/cereal/log.capnp').read_text().split('enum EventName', 1)[1].split('\n  }', 1)[0]
  assert core.ALERTS == set(re.findall(r'^\s*([A-Za-z][A-Za-z0-9_]*)\s+@\d+', text, re.M))
  assert 'selfdrived' in core.PROCESSES and 'driverDistracted' in core.CONDITIONS


class FakeSM(dict):
  def __init__(self, gear='drive', active=True):
    super().__init__(carState=NS(canValid=True, canTimeout=False, vEgoRaw=0., vEgo=0., standstill=True, gearShifter=gear),
      carParams=NS(notCar=False, carFingerprint='KIA_PV5'), onroadEvents=[])
    self.valid = self.alive = dict.fromkeys(self, True)
    self.seen = dict.fromkeys(self, True)
    self.logMonoTime = {'carState': 100_000_000_000}


def reporter(tmp_path, monkeypatch, connection=None):
  monkeypatch.setattr(runtime.time, 'monotonic', lambda: 100.1)
  monkeypatch.setattr(runtime.time, 'time', lambda: 1000.)
  r = runtime.Reporter(FakeSM(), lambda: True, NS(hostname='example.ts.net', port=443), b'x' * 32,
                       spool=tmp_path / 'pending.json', connection=connection)
  r.proof = proof()
  return r


def test_observer_counts_warning_edges_not_every_sample_and_keeps_stale_state(tmp_path, monkeypatch):
  r = reporter(tmp_path, monkeypatch)
  e = NS(name='modeldLagging', warning=True, noEntry=False, softDisable=False, immediateDisable=False, permanent=False)
  r.sm['onroadEvents'] = [e]
  for _ in range(100): r.observe(r.sm)
  assert len(r.queue.rows) == 1 and next(iter(r.queue.rows.values()))['count'] == 1
  r.sm.valid['onroadEvents'] = False; r.observe(r.sm)
  r.sm.valid['onroadEvents'] = True; r.observe(r.sm)
  assert next(iter(r.queue.rows.values()))['count'] == 1
  r.sm['onroadEvents'] = []; r.observe(r.sm)
  r.sm['onroadEvents'] = [e]; r.observe(r.sm)
  assert next(iter(r.queue.rows.values()))['count'] == 2


def test_checkpoint_only_at_zero_and_does_not_rewrite_unchanged_data(tmp_path, monkeypatch):
  r = reporter(tmp_path, monkeypatch); r.add(fact())
  r.proof = proof(raw_speed=.1, standstill=False)
  r.checkpoint(100.1); assert not r.spool.exists()
  r.proof = proof(); r.checkpoint(100.1)
  stamp = r.spool.stat().st_mtime_ns
  r.proof = proof(stamp=131.)
  r.checkpoint(131.1)
  assert r.spool.stat().st_mtime_ns == stamp and r.spool.stat().st_size <= core.MAX_SPOOL


class Connection:
  def __init__(self, on_connect=lambda: None, failure=False):
    self.on_connect, self.failure, self.data = on_connect, failure, None
    self.status, self.closed = 200, False
  def connect(self): self.on_connect()
  def request(self, method, path, body, headers):
    assert method == 'POST' and path == '/v1/incident'
    self.data, self.headers = body, headers
    if self.failure: raise OSError('offline')
  def getresponse(self): return self
  def read(self, size): return core.encode({'accepted': hashlib.sha256(self.data).hexdigest()})
  def close(self): self.closed = True


def test_upload_cancels_if_vehicle_moves_during_connect(tmp_path, monkeypatch):
  c = Connection()
  r = reporter(tmp_path, monkeypatch, lambda: c); r.add(fact())
  def move():
    r.proof = proof(raw_speed=.1, standstill=False)
    r.cancel()
  c.on_connect = move
  assert not r.send_once(100.1) and c.data is None and c.closed
  assert r.queue.rows


def test_successful_upload_is_small_signed_and_acknowledged(tmp_path, monkeypatch):
  c = Connection()
  r = reporter(tmp_path, monkeypatch, lambda: c); r.add(fact())
  assert r.send_once(100.1) and not r.queue.rows
  assert len(c.data) <= core.MAX_BODY
  assert core.authenticated(b'x' * 32, int(c.headers['X-PV5-Time']), c.headers['X-PV5-Nonce'],
                            c.headers['X-PV5-Signature'], c.data, 1000.)


def test_network_failure_keeps_bounded_queue_and_does_not_retry_tightly(tmp_path, monkeypatch):
  c = Connection(failure=True)
  r = reporter(tmp_path, monkeypatch, lambda: c); r.add(fact())
  assert not r.send_once(100.1) and r.queue.rows
  assert r.retry.next_at == 102.1 and not r.send_once(100.2)


def test_off_has_no_runtime_import_thread_subscriber_or_config_read(tmp_path):
  import subprocess, sys
  script = '''
import sys, types
sys.path.insert(0, sys.argv[1])
module = types.ModuleType('openpilot.common.params')
module.Params = lambda: types.SimpleNamespace(get_bool=lambda key: False)
sys.modules[module.__name__] = module
from openpilot.system.pv5_incidents import start_incident_reporting
assert start_incident_reporting() is None
assert 'openpilot.system.pv5_incidents.runtime' not in sys.modules
'''
  result = subprocess.run([sys.executable, '-c', script, str(ROOT)], capture_output=True)
  assert result.returncode == 0, result.stderr.decode()


def test_private_config_requires_https_fixed_path_and_strict_permissions(tmp_path):
  import os
  if os.name == 'nt':
    pytest.skip('POSIX device configuration permissions')
  p = tmp_path / 'private.json'
  value = {'endpoint': 'https://device.example.ts.net:10000/v1/incident', 'key': 'ab' * 32}
  p.write_text(json.dumps(value)); p.chmod(0o600)
  target, key = runtime.read_config(p)
  assert target.port == 10000 and len(key) == 32
  for endpoint in ('http://device.example.ts.net/v1/incident', 'https://evil.invalid/v1/incident',
                   'https://device.example.ts.net/other', 'https://user@device.example.ts.net/v1/incident',
                   'https://device.example.ts.net/v1/incident?token=x'):
    p.write_text(json.dumps({**value, 'endpoint': endpoint}))
    with pytest.raises(ValueError): runtime.read_config(p)
  p.write_text(json.dumps(value)); p.chmod(0o644)
  with pytest.raises(ValueError): runtime.read_config(p)
  p.chmod(0o600)
  link = tmp_path / 'link'; link.symlink_to(p)
  with pytest.raises(ValueError): runtime.read_config(link)


def test_real_http_authenticated_roundtrip_rejects_unauthorized_replay_and_oversize(tmp_path):
  import http.client
  import threading
  import time
  key = b'x' * 32
  inbox = receiver.Inbox(tmp_path / 'http.db')
  server = receiver.HTTPServer(('127.0.0.1', 0), receiver.handler(receiver.Ingress(key, inbox)))
  worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
  worker.start()
  def post(data, signed):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2.)
    try:
      connection.request('POST', '/v1/incident', body=data, headers=signed)
      response = connection.getresponse(); body = response.read()
      return response.status, body
    finally:
      connection.close()
  try:
    data = payload(); signed = headers(key, data, stamp=int(time.time()))
    assert post(data, {})[0] == 403
    status, answer = post(data, signed)
    assert status == 200 and json.loads(answer) == {'accepted': hashlib.sha256(data).hexdigest()}
    assert post(data, signed)[0] == 403
    assert post(b'x' * (core.MAX_BODY + 1), {})[0] == 413
    assert len(inbox.pending(int(time.time()))) == 1
  finally:
    server.shutdown(); server.server_close(); worker.join(timeout=2.)


def test_receiver_queue_full_is_atomic_and_still_bounded(tmp_path):
  inbox = receiver.Inbox(tmp_path / 'full.db')
  for i in range(64):
    q = core.Pending(); q.add(fact(source_commit=f'{i:040x}'), 1000.)
    inbox.accept(core.encode(q.batch(1000.)), 1000)
  q = core.Pending(); q.add(fact(source_commit='f' * 40), 1000.)
  with pytest.raises(ValueError, match='inbox_full'):
    inbox.accept(core.encode(q.batch(1000.)), 1000)
  with inbox.open() as db:
    assert db.execute('SELECT count(*) FROM incidents').fetchone()[0] == 64
    assert db.execute('SELECT count(*) FROM seen').fetchone()[0] == 64
  assert (tmp_path / 'full.db').stat().st_size <= 2 * 1024 * 1024


def test_coded_catalog_processes_match_existing_configuration():
  import re
  source = (ROOT / 'openpilot/system/manager/process_config.py').read_text()
  names = set(re.findall(r'(?:PythonProcess|NativeProcess|DaemonProcess)\("([^"]+)"', source))
  assert names == core.PROCESSES


@pytest.mark.parametrize('data', [b'[]', b'null', b'{}', b'{"rows":[]}'])
def test_corrupt_spool_cannot_replace_the_existing_queue(data):
  q = core.Pending(); q.add(fact(), 1000.); before = q.dump()
  with pytest.raises(ValueError): q.restore(data, 1000.)
  assert q.dump() == before


def test_full_disk_keeps_ram_queue_and_network_delivery(tmp_path, monkeypatch):
  c = Connection()
  r = reporter(tmp_path, monkeypatch, lambda: c); r.add(fact())
  def unavailable(*args, **kwargs):
    raise OSError('PC simulated full disk')
  monkeypatch.setattr(runtime.os, 'open', unavailable)
  r.checkpoint(100.1)
  assert r.queue.rows and r.next_checkpoint == 130.1
  assert r.send_once(100.1) and not r.queue.rows


def test_pc_cli_reads_utf8_config_and_unicode_database_path(tmp_path):
  import subprocess
  import sys
  config = tmp_path / '설정.json'
  config.write_text(json.dumps({'database': str(tmp_path / '제보함.sqlite3'), 'key': '00' * 32, 'port': 0},
                               ensure_ascii=False), encoding='utf-8')
  result = subprocess.run([sys.executable, str(ROOT / 'tools/pv5_incidents/receiver.py'),
                           '--config', str(config), 'pending'], capture_output=True, timeout=10.)
  assert result.returncode == 0, result.stderr.decode(errors='replace')
  assert json.loads(result.stdout) == {'pending': []}


@pytest.mark.parametrize('uncertain', [False, True])
def test_discord_receipt_is_rechecked_and_uncertain_post_is_not_repeated(tmp_path, uncertain):
  from tools.pv5_incidents.discord_report import Discord, notify_once
  inbox = receiver.Inbox(tmp_path / 'discord.db'); inbox.accept(payload(), 1000)
  sender = object.__new__(Discord)
  sender.channel, sender.guild, sender.bot = '123', '456', '789'
  sender.receipt = tmp_path / 'receipt.json'
  posts, messages = [], []
  def request(route, body=None):
    if body is not None:
      posts.append(body)
      messages.append({'id': '123456789012345678', 'channel_id': '123', 'content': body['content'],
                       'author': {'id': '789'}, 'attachments': []})
      if uncertain and len(posts) == 1:
        raise TimeoutError('PC simulated lost POST response')
      return messages[-1]
    if '?limit=' in route:
      return messages
    if '/messages/' in route:
      return messages[-1]
    return {'guild_id': '456'}
  sender.request = request
  if uncertain:
    with pytest.raises(TimeoutError): notify_once(inbox, sender)
    assert inbox.notifications()
  assert notify_once(inbox, sender)
  assert not inbox.notifications() and not notify_once(inbox, sender)
  assert len(posts) == 1 and posts[0]['allowed_mentions'] == {'parse': []}
  assert json.loads(sender.receipt.read_text())['verified'] is True
