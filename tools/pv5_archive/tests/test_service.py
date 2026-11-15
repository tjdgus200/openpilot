import json
from pathlib import Path
import sys
import tempfile
import unittest

from openpilot.system.pv5_incidents.archive_protocol import ZERO_CHAIN, advance, encode
from tools.pv5_archive.dispatch import ArchiveDispatch
from tools.pv5_archive.service import AnalysisQueue
from tools.pv5_archive.store import Store
from tools.pv5_archive.tests.test_store import KEY, description
from tools.pv5_archive.schema import full_schema


class ServiceChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='pv5-analysis-events-')
    self.root = Path(self.temp.name)
    self.store = Store(self.root / 'private', KEY, reserve=0)

  def tearDown(self):
    self.temp.cleanup()

  def result(self, token, status='review_candidates', kind='hard_deceleration'):
    self.store.analyzed(token, {'status': status, 'candidates': [{'kind': kind}] if kind else [], 'missing_services': []})

  def test_actual_bounded_pc_child_video_only_reports_insufficient_not_success(self):
    value = description({'fcamera.hevc': b'video_only'})
    self.store.begin(encode(value))
    entry = value['files'][0]
    self.store.chunk(entry['file_id'], 0, b'video_only')
    self.store.seal(entry['file_id'], encode({'size': entry['size'], 'chain': advance(ZERO_CHAIN, b'video_only')}))
    notify = []
    queue = AnalysisQueue(self.store, sys.executable, Path(__file__).resolve().parents[3], lambda: notify.append(True))
    self.assertTrue(queue.step())
    self.assertFalse(queue.step())
    self.assertEqual(notify, [True])
    with self.store.db() as db:
      value = json.loads(db.execute('SELECT summary FROM results').fetchone()[0])
    self.assertEqual(value['status'], 'insufficient_data')
    self.assertEqual(value['missing_services'], ['rlog'])

  def test_normal_upload_is_quiet_and_accepted_event_is_not_resent(self):
    sent = []
    dispatch = ArchiveDispatch(
      self.store,
      self.root / 'events',
      lambda: {'enabled': True, 'push_policy': 'analysis_only'},
      lambda path, _: sent.append(json.loads(path.read_bytes())) or {'status': 'accepted'},
      clock=lambda: 10000,
    )
    self.result('a' * 64, 'no_threshold_candidate', None)
    dispatch.step()
    self.assertEqual(sent, [])
    self.assertIsNone(dispatch.next_due)
    self.result('b' * 64)
    dispatch.step()
    dispatch.step()
    self.assertEqual(len(sent), 1)
    self.assertEqual(sent[0]['kind'], 'archive_analysis')
    self.assertIsNone(dispatch.next_due)

  def test_actual_bounded_child_full_schema_and_recorded_source_identity(self):
    import zstandard

    with full_schema() as schema:
      events = []
      init = schema.Event.new_message(valid=True, logMonoTime=1000000000)
      init.init('initData').gitCommit = 'd' * 40
      events.append(init.to_bytes())
      cp = schema.Event.new_message(valid=True, logMonoTime=100000000000)
      cp.init('carParams').carFingerprint = 'KIA_PV5'
      events.append(cp.to_bytes())
      for index in range(20):
        event = schema.Event.new_message(valid=True, logMonoTime=100000000000 + index * 50000000)
        cs = event.init('carState')
        cs.canValid, cs.vEgo, cs.aEgo = True, 15.0, -4.0
        events.append(event.to_bytes())
      data = zstandard.ZstdCompressor().compress(b''.join(events))
    value = description({'rlog.zst': data})
    self.store.begin(encode(value))
    entry = value['files'][0]
    self.store.chunk(entry['file_id'], 0, data)
    self.store.seal(entry['file_id'], encode({'size': len(data), 'chain': advance(ZERO_CHAIN, data)}))
    queue = AnalysisQueue(self.store, sys.executable, Path(__file__).resolve().parents[3])
    self.assertTrue(queue.step())
    with self.store.db() as db:
      summary = json.loads(db.execute('SELECT summary FROM results').fetchone()[0])
    self.assertEqual(summary['status'], 'insufficient_data')
    self.assertEqual(summary['source_commit'], 'd' * 40)
    self.assertEqual(summary['topic_counts']['carState'], 20)
    self.assertFalse(summary['road_video_present'])

  def test_cause_cooldown_and_three_per_five_minutes_with_queued_only_timer(self):
    now, sent = [10000], []
    dispatch = ArchiveDispatch(
      self.store,
      self.root / 'events',
      lambda: {'enabled': True, 'push_policy': 'analysis_only'},
      lambda path, _: sent.append(path) or {'status': 'accepted'},
      clock=lambda: now[0],
    )
    self.result('a' * 64)
    dispatch.step()
    self.result('b' * 64)
    now[0] += 301
    dispatch.step()
    self.assertEqual(len(sent), 1)
    self.assertEqual(dispatch.next_due, 11800)
    now[0] = 11800
    dispatch.step()
    self.assertEqual(len(sent), 2)
    for index in range(5):
      self.result(str(index) * 64, kind='candidate_' + str(index))
    now[0] += 301
    dispatch.step()
    event = json.loads(sent[-1].read_bytes())
    self.assertEqual(len(event['items']), 3)
    dispatch.step()
    self.assertEqual(dispatch.next_due, now[0] + 300)

  def test_uncertain_held_and_definitely_not_sent_retryable(self):
    calls = []
    dispatch = ArchiveDispatch(
      self.store,
      self.root / 'events',
      lambda: {'enabled': True, 'push_policy': 'analysis_only'},
      lambda *_: calls.append(True) or {'status': 'uncertain'},
      clock=lambda: 10000,
    )
    self.result('a' * 64)
    dispatch.step()
    dispatch.step()
    self.assertEqual(calls, [True])
    self.result('b' * 64, kind='other')
    dispatch.send = lambda *_: {'status': 'not_sent'}
    dispatch.clock = lambda: 12000
    dispatch.step()
    with self.store.db() as db:
      self.assertEqual(db.execute('SELECT notified FROM results WHERE segment_id=?', ('b' * 64,)).fetchone()[0], 0)
    self.assertEqual(dispatch.next_due, 12060)


if __name__ == '__main__':
  unittest.main()
