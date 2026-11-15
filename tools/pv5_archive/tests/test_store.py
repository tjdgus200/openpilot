import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from openpilot.system.pv5_incidents.archive_protocol import CHUNK, PRIVACY, ZERO_CHAIN, advance, encode, file_id, headers, manifest
from tools.pv5_archive.store import BoundedHTTPServer, CapacityError, Ingress, OffsetError, Store, handler

KEY = bytes(range(32))


def description(contents, segment='00000341--1810673eb4--14'):
  return {
    'version': 1,
    'privacy': PRIVACY,
    'segment': segment,
    'uploader_commit': 'a' * 40,
    'files': [
      {'name': name, 'size': len(data), 'mtime_ns': 1234567890, 'file_id': file_id(KEY, segment, name, len(data), 1234567890)}
      for name, data in contents.items()
    ],
  }


class StoreChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='pv5-archive-store-test-')
    self.root = Path(self.temp.name)
    self.store = Store(self.root, KEY, quota=10 * CHUNK, reserve=0)

  def tearDown(self):
    self.temp.cleanup()

  def upload(self, contents):
    value = description(contents)
    status = self.store.begin(encode(value))
    for entry in value['files']:
      data, token, chain = contents[entry['name']], entry['file_id'], ZERO_CHAIN
      for offset in range(0, len(data), CHUNK):
        body = data[offset : offset + CHUNK]
        result = self.store.chunk(token, offset, body)
        chain = advance(chain, body)
        self.assertEqual(result['chain'], chain)
      self.store.seal(token, encode({'size': len(data), 'chain': chain}))
    return value, status['segment_id']

  def test_originals_resume_seal_and_single_analysis_claim(self):
    contents = {'rlog.zst': b'r' * (CHUNK + 17), 'fcamera.hevc': b'v' * 111}
    value, segment = self.upload(contents)
    again = self.store.begin(encode(value))
    self.assertTrue(again['complete'])
    self.assertTrue(all(item['complete'] for item in again['files']))
    job = self.store.claim()
    self.assertEqual(job['segment_id'], segment)
    self.assertEqual({name: Path(path).read_bytes() for name, path in job['files'].items()}, contents)
    self.assertIsNone(self.store.claim())
    self.store.analyzed(segment, {'status': 'analyzed', 'candidates': []})
    self.store.begin(encode(value))
    self.assertIsNone(self.store.claim())

  def test_lost_ack_duplicate_and_conflicting_duplicate(self):
    value = description({'rlog.zst': b'r' * (CHUNK + 1)})
    token = value['files'][0]['file_id']
    self.store.begin(encode(value))
    first = self.store.chunk(token, 0, b'r' * CHUNK)
    self.assertEqual(self.store.chunk(token, 0, b'r' * CHUNK), first)
    with self.assertRaises(ValueError):
      self.store.chunk(token, 0, b'x' * CHUNK)
    restarted = Store(self.root, KEY, quota=10 * CHUNK, reserve=0)
    state = restarted.begin(encode(value))['files'][0]
    self.assertEqual(state['offset'], CHUNK)
    self.assertEqual(state['chain'], advance(ZERO_CHAIN, b'r' * CHUNK))

  def test_ahead_offset_and_unaligned_or_wrong_chunk_size(self):
    value = description({'rlog.zst': b'r' * (CHUNK + 5)})
    token = value['files'][0]['file_id']
    self.store.begin(encode(value))
    with self.assertRaises(OffsetError):
      self.store.chunk(token, CHUNK, b'r' * 5)
    for offset, body in ((1, b'x' * CHUNK), (0, b'x' * (CHUNK - 1)), (0, b'x' * (CHUNK + 1))):
      with self.assertRaises(ValueError):
        self.store.chunk(token, offset, body)

  def test_no_analysis_until_all_originals_are_verified(self):
    value = description({'rlog.zst': b'r', 'ecamera.hevc': b'v'})
    self.store.begin(encode(value))
    for entry in value['files']:
      body = b'r' if entry['name'] == 'rlog.zst' else b'v'
      self.store.chunk(entry['file_id'], 0, body)
      self.assertIsNone(self.store.claim())
    first = value['files'][0]
    self.store.seal(first['file_id'], encode({'size': 1, 'chain': advance(ZERO_CHAIN, b'r')}))
    self.assertIsNone(self.store.claim())
    last = value['files'][1]
    self.store.seal(last['file_id'], encode({'size': 1, 'chain': advance(ZERO_CHAIN, b'v')}))
    self.assertIsNotNone(self.store.claim())

  def test_corrupted_disk_or_incomplete_seal_is_rejected(self):
    value = description({'rlog.zst': b'r' * 3})
    token = value['files'][0]['file_id']
    self.store.begin(encode(value))
    with self.assertRaises(ValueError):
      self.store.seal(token, encode({'size': 3, 'chain': ZERO_CHAIN}))
    result = self.store.chunk(token, 0, b'rrr')
    (self.root / 'objects' / token / 'rlog.zst.part').write_bytes(b'xxx')
    with self.assertRaises(ValueError):
      self.store.seal(token, encode({'size': 3, 'chain': result['chain']}))
    self.assertIsNone(self.store.claim())

  def test_only_unacknowledged_partial_tail_is_truncated(self):
    value = description({'rlog.zst': b'r' * (CHUNK + 2)})
    token = value['files'][0]['file_id']
    self.store.begin(encode(value))
    self.store.chunk(token, 0, b'r' * CHUNK)
    path = self.root / 'objects' / token / 'rlog.zst.part'
    with path.open('ab') as stream:
      stream.write(b'unacknowledged')
    self.store.chunk(token, CHUNK, b'rr')
    self.assertEqual(path.read_bytes(), b'r' * (CHUNK + 2))

  def test_storage_reservation_and_free_space_floor(self):
    store = Store(self.root / 'small', KEY, quota=10, reserve=0)
    with self.assertRaises(CapacityError):
      store.begin(encode(description({'rlog.zst': b'x' * 11})))
    with patch('tools.pv5_archive.store.shutil.disk_usage', return_value=type('Usage', (), {'free': 1})()):
      with self.assertRaises(CapacityError):
        self.store.begin(encode(description({'rlog.zst': b'x' * 2})))

  def test_forbidden_names_paths_and_identity_tampering(self):
    for name in ('dcamera.hevc', 'audio.wav', 'pc.json', '../rlog.zst', '/rlog.zst', 'rlog.zst/../../x'):
      with self.assertRaises(ValueError):
        manifest(KEY, description({name: b'x'}))
    for field, bad in (('file_id', '0' * 64), ('size', True), ('size', 0), ('mtime_ns', -1)):
      value = description({'rlog.zst': b'x'})
      value['files'][0][field] = bad
      with self.assertRaises(ValueError):
        self.store.begin(encode(value))

  def test_symlinks_cannot_redirect_pc_originals(self):
    value = description({'rlog.zst': b'r'})
    token = value['files'][0]['file_id']
    elsewhere = self.root / 'other'
    elsewhere.mkdir()
    (self.root / 'objects' / token).symlink_to(elsewhere, target_is_directory=True)
    self.store.begin(encode(value))
    with self.assertRaises(ValueError):
      self.store.chunk(token, 0, b'r')
    self.assertEqual(list(elsewhere.iterdir()), [])

  def test_recover_only_interrupted_analysis_and_bound_error(self):
    _, token = self.upload({'rlog.zst': b'r'})
    self.assertIsNotNone(self.store.claim())
    self.store.recover()
    self.assertIsNotNone(self.store.claim())
    self.store.failed(token, 'bad\nraw private text')
    with self.store.db() as db:
      self.assertEqual(db.execute('SELECT error FROM segments WHERE id=?', (token,)).fetchone()[0], 'analysis_failed')


class AuthChecks(unittest.TestCase):
  def test_signature_covers_endpoint_offset_and_body_and_rejects_replay(self):
    with tempfile.TemporaryDirectory() as directory:
      store = Store(Path(directory), KEY, reserve=0)
      ingress = Ingress(store)
      body = encode(description({'rlog.zst': b'x'}))
      path, now = '/v1/archive/manifest', 1_700_000_000
      signed = headers(KEY, path, body, now=now)
      ingress.ingest(path, signed, body, now)
      for altered_path, altered_body, altered_now in ((path, body, now), (path + '/x', body, now), (path, body + b'x', now), (path, body, now + 301)):
        with self.assertRaises(PermissionError):
          ingress.ingest(altered_path, signed, altered_body, altered_now)


class HttpChecks(unittest.TestCase):
  def test_http_chunks_and_legacy_incident_endpoint_stay_separate(self):
    from tools.pv5_incidents.receiver import Inbox, Ingress as OldIngress, handler as old_handler
    from openpilot.system.pv5_incidents.core import signature

    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      store = Store(root / 'originals', KEY, reserve=0)
      server = BoundedHTTPServer(('127.0.0.1', 0), handler(old_handler(OldIngress(KEY, Inbox(root / 'old.sqlite'))), Ingress(store)))
      thread = threading.Thread(target=server.serve_forever, daemon=True)
      thread.start()
      connection = http.client.HTTPConnection(*server.server_address, timeout=3)
      try:
        body = encode(description({'rlog.zst': b'x'}))
        path = '/v1/archive/manifest'
        connection.request('POST', path, body, headers=headers(KEY, path, body))
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(json.loads(response.read())['files']), 1)
        connection.request('POST', '/v1/incident', b'x' * 8193)
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        now, nonce, body = int(__import__('time').time()), 'a' * 32, b'{"probe":1}'
        connection.request(
          'POST', '/v1/incident', body, headers={'X-PV5-Time': str(now), 'X-PV5-Nonce': nonce, 'X-PV5-Signature': signature(KEY, now, nonce, body)}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 422)
        response.read()
      finally:
        connection.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
  unittest.main()
