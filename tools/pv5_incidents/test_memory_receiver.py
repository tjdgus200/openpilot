"""Compatibility checks for the PC receiver's separate PSS payload."""
import hashlib
import http.client
import json
import secrets
import threading
import time

from openpilot.system.pv5_incidents.core import Pending, encode, signature
from tools.pv5_incidents.receiver import HTTPServer, Inbox, Ingress, handler


def memory(now, **changes):
  return {'format': 2, 'id': secrets.token_hex(16), 'captured_at': now, 'fact': {
    'vehicle': 'KIA_PV5', 'kind': 'memory_pressure', 'process': 'proclogd', 'code': 'memoryPressure',
    'source_commit': 'unknown', 'severity': 1, 'usage_percent': 85,
    'pss': [{'name': 'modeld', 'bytes': 1024, 'age_ms': 23}],
  } | changes}


def test_signed_http_accepts_legacy_and_memory_without_triggering_analysis(tmp_path):
  now = int(time.time())
  key = b'a' * 32
  inbox = Inbox(tmp_path / 'incidents.db')
  server = HTTPServer(('127.0.0.1', 0), handler(Ingress(key, inbox)))
  worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
  worker.start()

  def post(value, signed=True):
    data = encode(value)
    nonce = secrets.token_hex(16)
    headers = {'X-PV5-Time': str(now), 'X-PV5-Nonce': nonce, 'X-PV5-Signature': signature(key, now, nonce, data)}
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    try:
      connection.request('POST', '/v1/incident', body=data, headers=headers if signed else {})
      response = connection.getresponse()
      result = response.read()
      if response.status == 200:
        assert json.loads(result) == {'accepted': hashlib.sha256(data).hexdigest()}
      return response.status
    finally:
      connection.close()

  try:
    capture = memory(now)
    assert post(capture, signed=False) == 403
    assert post(memory(now, usage_percent=84)) == 422
    assert post(memory(now, pss=[{'name': 'modeld', 'bytes': 1024, 'age_ms': 0, 'cmdline': 'private'}])) == 422
    assert post(capture) == 200
    assert post(capture) == 200
    assert inbox.memory() == [{'captured_at': now, **capture['fact']}]
    assert not inbox.pending(now) and not inbox.notifications()

    legacy = Pending()
    legacy.add({'vehicle': 'KIA_PV5', 'kind': 'system_alert', 'process': 'selfdrived', 'code': 'lowMemory',
                'source_commit': 'unknown', 'severity': 1}, now)
    assert post(legacy.batch(now)) == 200
    assert len(inbox.pending(now)) == 1
  finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def test_memory_retention_has_its_own_limit(tmp_path):
  inbox = Inbox(tmp_path / 'incidents.db')
  for now in range(1000, 1040):
    inbox.accept(encode(memory(now)), now)
  assert [row['captured_at'] for row in inbox.memory()] == list(range(1039, 1007, -1))
