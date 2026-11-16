"""Only meaningful archive results wake the existing analyst chat."""

import hashlib
import json
from pathlib import Path
import threading
import time

from openpilot.system.pv5_incidents.archive_protocol import encode
from tools.pv5_archive.service import atomic
from openpilot.system.pv5_incidents.priority import archive_reviewable, fault_key


def causes(summary):
  kinds = sorted({value['kind'] for value in summary.get('candidates', [])})
  base = [kinds, summary.get('status'), summary.get('missing_services', [])]
  legacy = hashlib.sha256(encode(base)).hexdigest()
  current = hashlib.sha256(encode(base + [sorted(summary.get('error_codes', [])),
                                         summary.get('source_commit', 'unknown')])).hexdigest()
  return current, legacy


def encoded_fault(summary):
  key = fault_key(summary)
  return encode(key).decode() if key is not None else None


class ArchiveDispatch:
  def __init__(self, store, folder, policy, send, clock=time.time):
    self.store, self.folder, self.policy, self.send, self.clock = store, Path(folder), policy, send, clock
    self.folder.mkdir(exist_ok=True)
    self.event, self.stopping, self.next_due = threading.Event(), False, None
    with store.db() as db:
      db.execute('CREATE TABLE IF NOT EXISTS dispatches(id TEXT PRIMARY KEY, cause TEXT, stamp REAL, status TEXT)')
      # Early log failure and later full analysis can replace results.summary.
      # Deduplication must remember what was actually dispatched, not that new result.
      db.execute('CREATE TABLE IF NOT EXISTS dispatch_facts('
                 'id TEXT, cause TEXT, stamp REAL, status TEXT, fault_key TEXT, PRIMARY KEY(id,cause))')

  def wake(self):
    self.event.set()

  def step(self):
    self.next_due = None
    policy, now = self.policy(), self.clock()
    if policy.get('enabled') is not True or policy.get('push_policy') != 'analysis_only':
      return
    with self.store.lock, self.store.db() as db:
      rows = db.execute('SELECT * FROM results WHERE notified=0 ORDER BY stamp LIMIT 64').fetchall()
      history_sql = ("SELECT id,cause,stamp,status FROM dispatch_facts UNION ALL "
                     "SELECT d.id,d.cause,d.stamp,d.status FROM dispatches d WHERE NOT EXISTS "
                     "(SELECT 1 FROM dispatch_facts f WHERE f.id=d.id AND f.cause=d.cause)")
      last = db.execute("SELECT max(stamp) FROM (" + history_sql + ") WHERE status!='not_sent'").fetchone()[0] or 0
      history = db.execute(
        "SELECT h.*,f.fault_key,f.id AS snapshot_id,r.summary FROM (" + history_sql + ") h "
        "LEFT JOIN dispatch_facts f ON f.id=h.id AND f.cause=h.cause "
        "LEFT JOIN results r ON r.segment_id=h.id WHERE h.status!='not_sent' ORDER BY h.stamp DESC LIMIT 512"
      ).fetchall()
      reviewed_faults = set()
      for item in history:
        key = item['fault_key']
        if item['snapshot_id'] is None and item['summary'] is not None:
          old = json.loads(item['summary'])
          # Old databases lack snapshots. Only use a result still matching the sent cause.
          key = encoded_fault(old) if item['cause'] in causes(old) else None
        if key is not None:
          reviewed_faults.add(key)
      selected, deadlines, selected_causes = [], [], []
      for row in rows:
        summary = json.loads(row['summary'])
        if summary.get('awaiting_media') is True and summary.get('status') != 'analysis_failed':
          db.execute('UPDATE results SET notified=-2 WHERE segment_id=?', (row['segment_id'],))
          continue  # The later full manifest reuses data screening, then opens visual review.
        prior = db.execute('SELECT status FROM dispatches WHERE id=?', (row['segment_id'],)).fetchone()
        if prior is not None and prior['status'] not in ('accepted', 'not_sent'):
          db.execute('UPDATE results SET notified=1 WHERE segment_id=?', (row['segment_id'],))
          continue  # Hold an uncertain existing intent; reconcile its receipt before any resend.
        key = encoded_fault(summary)
        if not archive_reviewable(summary) or (key is not None and key in reviewed_faults):
          db.execute('UPDATE results SET notified=-1 WHERE segment_id=?', (row['segment_id'],))
          continue  # No AI for ordinary candidates or a known same-version fault.
        kinds = sorted({value['kind'] for value in summary.get('candidates', [])})
        screened_quiet = summary.get('screening_policy') == 'risk-first-v1' and summary.get('status') != 'analysis_failed'
        if not kinds and (summary.get('status') == 'no_threshold_candidate' or screened_quiet):
          db.execute('UPDATE results SET notified=-1 WHERE segment_id=?', (row['segment_id'],))
          continue  # Ordinary uploads never produce a chat.
        cause, legacy_cause = causes(summary)
        previous = db.execute("SELECT max(stamp) FROM (" + history_sql +
                              ") WHERE cause IN (?,?) AND status!='not_sent'", (cause, legacy_cause)).fetchone()[0] or 0
        due = max(last + 300 if last else 0, previous + 1800 if previous else 0)
        if due > now:
          deadlines.append(due)
          continue
        if cause in selected_causes or len(selected) >= 3:
          deadlines.append(now + 300)
          continue
        selected.append({'segment_id': row['segment_id'], 'cause': cause})
        selected_causes.append(cause)
        if key is not None:
          reviewed_faults.add(key)
      if not selected:
        self.next_due = min(deadlines) if deadlines else None
        return
      event_id = hashlib.sha256(encode(selected)).hexdigest()
      event_file = self.folder / (event_id + '.json')
      # Persist intent before the desktop call. Uncertain deliveries are held,
      # never blindly resubmitted after a process crash.
      for item in selected:
        db.execute('INSERT OR REPLACE INTO dispatches VALUES(?,?,?,?)', (item['segment_id'], item['cause'], now, 'dispatch_intent'))
        summary = json.loads(db.execute('SELECT summary FROM results WHERE segment_id=?', (item['segment_id'],)).fetchone()[0])
        db.execute('INSERT OR REPLACE INTO dispatch_facts VALUES(?,?,?,?,?)',
                   (item['segment_id'], item['cause'], now, 'dispatch_intent', encoded_fault(summary)))
        db.execute('UPDATE results SET notified=1 WHERE segment_id=?', (item['segment_id'],))
      atomic(event_file, {'kind': 'archive_analysis', 'event_id': event_id, 'items': selected})
    try:
      result = self.send(event_file, policy)
    except Exception:
      result = {'status': 'uncertain'}
    status = result.get('status', 'uncertain')
    with self.store.lock, self.store.db() as db:
      for item in selected:
        db.execute('UPDATE dispatches SET status=? WHERE id=?', (status, item['segment_id']))
        db.execute('UPDATE dispatch_facts SET status=? WHERE id=? AND cause=?', (status, item['segment_id'], item['cause']))
        if status == 'not_sent':
          db.execute('UPDATE results SET notified=0 WHERE segment_id=?', (item['segment_id'],))
    self.next_due = now + (60 if status == 'not_sent' else 300)

  def run(self):
    self.wake()
    while not self.stopping:
      timeout = None if self.next_due is None else max(0.0, self.next_due - self.clock())
      self.event.wait(timeout)
      self.event.clear()
      if not self.stopping:
        try:
          self.step()
        except Exception as error:
          self.next_due = None  # Fail visibly in local state; no empty retry chats.
          atomic(self.folder / 'dispatch-status.json', {'status': 'needs_attention', 'error_type': type(error).__name__})
