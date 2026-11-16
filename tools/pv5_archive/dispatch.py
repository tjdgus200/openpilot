"""Only meaningful archive results wake the existing analyst chat."""

import hashlib
import json
from pathlib import Path
import threading
import time

from openpilot.system.pv5_incidents.archive_protocol import encode
from tools.pv5_archive.service import atomic
from openpilot.system.pv5_incidents.priority import archive_reviewable, fault_key


class ArchiveDispatch:
  def __init__(self, store, folder, policy, send, clock=time.time):
    self.store, self.folder, self.policy, self.send, self.clock = store, Path(folder), policy, send, clock
    self.folder.mkdir(exist_ok=True)
    self.event, self.stopping, self.next_due = threading.Event(), False, None
    with store.db() as db:
      db.execute('CREATE TABLE IF NOT EXISTS dispatches(id TEXT PRIMARY KEY, cause TEXT, stamp REAL, status TEXT)')

  def wake(self):
    self.event.set()

  def step(self):
    self.next_due = None
    policy, now = self.policy(), self.clock()
    if policy.get('enabled') is not True or policy.get('push_policy') != 'analysis_only':
      return
    with self.store.lock, self.store.db() as db:
      rows = db.execute('SELECT * FROM results WHERE notified=0 ORDER BY stamp LIMIT 64').fetchall()
      last = db.execute("SELECT max(stamp) FROM dispatches WHERE status!='not_sent'").fetchone()[0] or 0
      history = db.execute("SELECT results.summary FROM dispatches JOIN results ON results.segment_id=dispatches.id "
                           "WHERE dispatches.status!='not_sent' ORDER BY dispatches.stamp DESC LIMIT 512").fetchall()
      reviewed_faults = {key for row in history if (key := fault_key(json.loads(row['summary']))) is not None}
      selected, deadlines, causes = [], [], []
      for row in rows:
        summary = json.loads(row['summary'])
        key = fault_key(summary)
        if not archive_reviewable(summary) or (key is not None and key in reviewed_faults):
          db.execute('UPDATE results SET notified=-1 WHERE segment_id=?', (row['segment_id'],))
          continue  # No AI for ordinary candidates or a known same-version fault.
        kinds = sorted({value['kind'] for value in summary.get('candidates', [])})
        screened_quiet = summary.get('screening_policy') == 'risk-first-v1' and summary.get('status') != 'analysis_failed'
        if not kinds and (summary.get('status') == 'no_threshold_candidate' or screened_quiet):
          db.execute('UPDATE results SET notified=-1 WHERE segment_id=?', (row['segment_id'],))
          continue  # Ordinary uploads never produce a chat.
        legacy_cause = hashlib.sha256(encode([kinds, summary.get('status'), summary.get('missing_services', [])])).hexdigest()
        cause = hashlib.sha256(encode([kinds, summary.get('status'), summary.get('missing_services', []),
                                      sorted(summary.get('error_codes', [])), summary.get('source_commit', 'unknown')])).hexdigest()
        previous = db.execute("SELECT max(stamp) FROM dispatches WHERE cause IN (?,?) AND status!='not_sent'", (cause, legacy_cause)).fetchone()[0] or 0
        due = max(last + 300 if last else 0, previous + 1800 if previous else 0)
        if due > now:
          deadlines.append(due)
          continue
        if cause in causes or len(selected) >= 3:
          deadlines.append(now + 300)
          continue
        selected.append({'segment_id': row['segment_id'], 'cause': cause})
        causes.append(cause)
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
