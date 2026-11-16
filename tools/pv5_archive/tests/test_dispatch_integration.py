"""Home early-screening and critical-only dispatch must work together."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.pv5_archive.dispatch import ArchiveDispatch, causes
from tools.pv5_archive.store import Store
from tools.pv5_archive.tests.test_store import KEY


class DispatchIntegrationChecks(unittest.TestCase):
  def setUp(self):
    self.temp = TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    root = Path(self.temp.name)
    self.store = Store(root / 'store', KEY, reserve=0)
    self.sent, self.now = [], 10000
    def send(path, policy):
      self.sent.append(json.loads(path.read_bytes()))
      return {'status': 'accepted'}
    self.dispatch = ArchiveDispatch(self.store, root / 'events',
      lambda: {'enabled': True, 'push_policy': 'analysis_only'}, send, clock=lambda: self.now)

  def summary(self, code=None, waiting=False):
    return {'status': 'review_candidates', 'screening_policy': 'risk-first-v1',
            'candidates': [{'kind': 'system_alert' if code else 'hard_deceleration'}],
            'error_codes': [code] if code else [], 'source_commit': 'a' * 40,
            'awaiting_media': waiting}

  def notified(self, digest):
    with self.store.db() as db:
      return db.execute('SELECT notified FROM results WHERE segment_id=?', (digest,)).fetchone()[0]

  def test_critical_waits_for_media_then_sends_once(self):
    digest = 'a' * 64
    self.store.analyzed(digest, self.summary('stockAeb', True))
    self.dispatch.step()
    self.assertEqual(self.sent, [])
    self.assertEqual(self.notified(digest), -2)
    self.store.analyzed(digest, self.summary('stockAeb'))
    self.dispatch.step()
    self.dispatch.step()
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.sent[0]['items'][0]['segment_id'], digest)

  def test_ordinary_early_and_complete_results_never_wake_ai(self):
    digest = 'b' * 64
    self.store.analyzed(digest, self.summary(waiting=True))
    self.dispatch.step()
    self.assertEqual(self.notified(digest), -2)
    self.store.analyzed(digest, self.summary())
    self.dispatch.step()
    self.assertEqual(self.notified(digest), -1)
    self.assertEqual(self.sent, [])
    self.assertIsNone(self.dispatch.next_due)

  def test_complete_same_version_fault_remains_deduplicated(self):
    self.store.analyzed('c' * 64, self.summary('controlsMismatch'))
    self.dispatch.step()
    self.now += 1801
    self.store.analyzed('d' * 64, self.summary('controlsMismatch', True))
    self.dispatch.step()
    self.assertEqual(self.notified('d' * 64), -2)
    self.store.analyzed('d' * 64, self.summary('controlsMismatch'))
    self.dispatch.step()
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.notified('d' * 64), -1)

  def test_analysis_failure_is_visible_before_media(self):
    self.store.analyzed('e' * 64, {'status': 'analysis_failed', 'failure': 'TimeoutExpired',
      'candidates': [], 'awaiting_media': True})
    self.dispatch.step()
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.notified('e' * 64), 1)

  def test_distinct_critical_episode_keeps_cooldown_not_fault_suppression(self):
    self.store.analyzed('1' * 64, self.summary('stockAeb'))
    self.dispatch.step()
    self.now += 301
    self.store.analyzed('2' * 64, self.summary('stockAeb'))
    self.dispatch.step()
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.dispatch.next_due, 11800)
    self.now = 11800
    self.dispatch.step()
    self.assertEqual(len(self.sent), 2)

  def test_failure_then_new_fault_same_digest_is_not_mistaken_for_reported(self):
    digest = '3' * 64
    failure = {'status': 'analysis_failed', 'failure': 'TimeoutExpired', 'candidates': [], 'awaiting_media': True}
    self.store.analyzed(digest, failure)
    self.dispatch.step()
    self.store.analyzed(digest, self.summary('controlsMismatch'))
    self.now += 301
    self.dispatch.step()
    self.assertEqual(len(self.sent), 2)
    with self.store.db() as db:
      self.assertEqual(db.execute('SELECT count(*) FROM dispatch_facts').fetchone()[0], 2)
    # Both original failure and later fault remain known after the result changed.
    self.now += 1801
    self.store.analyzed('4' * 64, failure)
    self.store.analyzed('5' * 64, self.summary('controlsMismatch'))
    self.dispatch.step()
    self.assertEqual(len(self.sent), 2)

  def test_uncertain_same_digest_never_overwrites_prior_intent(self):
    for status in ('uncertain', 'dispatch_intent'):
      with self.subTest(status=status):
        digest = ('6' if status == 'uncertain' else '7') * 64
        failure = {'status': 'analysis_failed', 'failure': status, 'candidates': [], 'awaiting_media': True}
        self.store.analyzed(digest, failure)
        self.dispatch.send = lambda *args: {'status': status}
        self.dispatch.step()
        with self.store.db() as db:
          before = tuple(db.execute('SELECT * FROM dispatches WHERE id=?', (digest,)).fetchone())
        self.store.analyzed(digest, self.summary('controlsMismatch'))
        self.now += 1801
        self.dispatch.step()
        with self.store.db() as db:
          self.assertEqual(tuple(db.execute('SELECT * FROM dispatches WHERE id=?', (digest,)).fetchone()), before)
        self.assertEqual(self.notified(digest), 1)

  def test_legacy_matching_fault_still_deduplicated(self):
    summary = self.summary('controlsMismatch')
    self.store.analyzed('8' * 64, summary)
    with self.store.db() as db:
      db.execute('INSERT INTO dispatches VALUES(?,?,?,?)', ('8' * 64, causes(summary)[0], 8000, 'accepted'))
      db.execute('UPDATE results SET notified=1')
    self.store.analyzed('9' * 64, summary)
    self.dispatch.step()
    self.assertEqual(self.sent, [])
    self.assertEqual(self.notified('9' * 64), -1)

  def test_legacy_replaced_failure_cannot_be_used_as_new_fault_evidence(self):
    failure = {'status': 'analysis_failed', 'candidates': []}
    digest = 'a' * 64
    self.store.analyzed(digest, self.summary('controlsMismatch'))
    with self.store.db() as db:
      db.execute('INSERT INTO dispatches VALUES(?,?,?,?)', (digest, causes(failure)[0], 8000, 'accepted'))
    self.dispatch.step()
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.notified(digest), 1)

  def test_definitely_unsent_fact_does_not_suppress_retry(self):
    digest = 'b' * 64
    self.store.analyzed(digest, self.summary('controlsMismatch'))
    self.dispatch.send = lambda *args: {'status': 'not_sent'}
    self.dispatch.step()
    self.assertEqual(self.notified(digest), 0)
    self.now += 61
    self.dispatch.send = lambda *args: self.sent.append(True) or {'status': 'accepted'}
    self.dispatch.step()
    self.assertEqual(self.sent, [True])
    self.assertEqual(self.notified(digest), 1)


if __name__ == '__main__':
  unittest.main()
