"""PC-only authenticated ingress. Loopback only; expose through HTTPS, never as a file server."""
import argparse
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from openpilot.system.pv5_incidents.core import MAX_BODY, MAX_COUNT, authenticated, encode, validate_batch


class Inbox:
  def __init__(self, path):
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with self.open() as db:
      db.executescript('''
        PRAGMA max_page_count=512;
        PRAGMA journal_mode=TRUNCATE;
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, revision INTEGER, total INTEGER, stamp INTEGER);
        CREATE TABLE IF NOT EXISTS incidents(
          digest TEXT PRIMARY KEY, fact TEXT, total INTEGER, first INTEGER, last INTEGER,
          notified INTEGER DEFAULT 0, report_count INTEGER DEFAULT 0, report_time INTEGER DEFAULT 0,
          lease_until INTEGER DEFAULT 0, lost INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reports(digest TEXT PRIMARY KEY, summary TEXT, stamp INTEGER, receipt TEXT);
      ''')

  @contextmanager
  def open(self):
    db = sqlite3.connect(self.path, timeout=2.)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=2000')
    db.execute('PRAGMA max_page_count=512')
    try:
      with db:
        yield db
    finally:
      db.close()

  def accept(self, data, now):
    batch = validate_batch(data)
    with self.open() as db:
      db.execute('BEGIN IMMEDIATE')
      db.execute('DELETE FROM seen WHERE stamp < ?', (now - 604800,))
      for row in batch['rows']:
        prior = db.execute('SELECT * FROM seen WHERE id=?', (row['id'],)).fetchone()
        if prior and row['revision'] <= prior['revision']:
          continue
        fact = encode(row['fact']).decode()
        digest = hashlib.sha256(fact.encode()).hexdigest()
        found = db.execute('SELECT * FROM incidents WHERE digest=?', (digest,)).fetchone()
        if not found:
          if db.execute('SELECT count(*) FROM incidents').fetchone()[0] >= 64:
            # Retire the oldest completed item, never an unreported fault.
            old = db.execute('SELECT digest FROM incidents WHERE report_count >= total ORDER BY last LIMIT 1').fetchone()
            if not old:
              raise ValueError('inbox_full')
            db.execute('DELETE FROM incidents WHERE digest=?', (old[0],))
          db.execute('INSERT INTO incidents(digest,fact,total,first,last) VALUES(?,?,0,?,?)',
                     (digest, fact, row['first'], row['last']))
        delta = row['count'] - prior['total'] if prior else row['count']
        if delta < 0:
          raise ValueError('counter_regression')
        db.execute('UPDATE incidents SET total=min(?,total+?),first=min(first,?),last=max(last,?),lost=max(lost,?) WHERE digest=?',
                   (MAX_COUNT, delta, row['first'], row['last'], batch['evicted'] + batch['rejected'], digest))
        db.execute('INSERT OR REPLACE INTO seen VALUES(?,?,?,?)', (row['id'], row['revision'], row['count'], now))
      db.execute('DELETE FROM seen WHERE id NOT IN (SELECT id FROM seen ORDER BY stamp DESC LIMIT 512)')
    return {'accepted': hashlib.sha256(data).hexdigest()}

  def pending(self, now, claim=False):
    with self.open() as db:
      db.execute('BEGIN IMMEDIATE')
      rows = db.execute('''SELECT * FROM incidents WHERE total>report_count AND lease_until<=?
        AND (report_time=0 OR report_time<=?) ORDER BY first LIMIT 3''', (now, now - 1800)).fetchall()
      result = []
      for row in rows:
        if claim:
          db.execute('UPDATE incidents SET lease_until=? WHERE digest=?', (now + 1200, row['digest']))
        result.append({**dict(row), 'fact': json.loads(row['fact'])})
      return result

  def notifications(self):
    with self.open() as db:
      rows = db.execute('SELECT digest,fact,total FROM incidents WHERE notified=0 ORDER BY first LIMIT 8').fetchall()
      return [{**dict(row), 'fact': json.loads(row['fact'])} for row in rows]

  def mark_notified(self, digests):
    if len(digests) > 8 or any(not re.fullmatch('[0-9a-f]{64}', d) for d in digests):
      raise ValueError('invalid_ids')
    with self.open() as db:
      db.executemany('UPDATE incidents SET notified=1 WHERE digest=?', [(d,) for d in digests])

  def complete(self, digest, count, summary, receipt, now):
    if (not re.fullmatch('[0-9a-f]{64}', digest) or type(count) is not int or not 1 <= count <= MAX_COUNT
        or not isinstance(summary, str) or len(summary) > 4000 or not re.fullmatch('[0-9]{15,22}', receipt)):
      raise ValueError('invalid_completion')
    with self.open() as db:
      row = db.execute('SELECT total FROM incidents WHERE digest=?', (digest,)).fetchone()
      if not row or count > row[0]:
        raise ValueError('unknown_completion')
      db.execute('UPDATE incidents SET report_count=max(report_count,?),report_time=?,lease_until=0 WHERE digest=?', (count, now, digest))
      db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?)', (digest, summary, now, receipt))
      db.execute('DELETE FROM reports WHERE digest NOT IN (SELECT digest FROM reports ORDER BY stamp DESC LIMIT 32)')


class Ingress:
  def __init__(self, key, inbox):
    self.key, self.inbox = key, inbox
    self.nonces = OrderedDict()

  def ingest(self, headers, data, now):
    stamp = headers.get('X-PV5-Time', '')
    nonce, digest = headers.get('X-PV5-Nonce', ''), headers.get('X-PV5-Signature', '')
    if not re.fullmatch('[0-9]{1,10}', stamp) or not authenticated(self.key, int(stamp), nonce, digest, data, now):
      raise PermissionError('unauthorized')
    while self.nonces and (len(self.nonces) >= 512 or next(iter(self.nonces.values())) < now - 300):
      self.nonces.popitem(last=False)
    if nonce in self.nonces:
      raise PermissionError('replay')
    result = self.inbox.accept(data, int(now))
    self.nonces[nonce] = now
    return result


def handler(ingress):
  class Handler(BaseHTTPRequestHandler):
    def setup(self):
      super().setup()
      self.connection.settimeout(2.)

    def log_message(self, *_):
      pass  # Never echo untrusted headers, URLs, IDs or payloads.

    def do_POST(self):
      if self.path != '/v1/incident' or self.headers.get('Transfer-Encoding'):
        self.send_error(404)
        return
      length = self.headers.get('Content-Length', '')
      if not re.fullmatch('[0-9]{1,5}', length) or not 0 < int(length) <= MAX_BODY:
        self.send_error(413)
        return
      try:
        body = self.rfile.read(int(length))
        result = ingress.ingest(self.headers, body, time.time())
      except PermissionError:
        self.send_error(403)
        return
      except (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error):
        self.send_error(422)
        return
      data = encode(result)
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(data)))
      self.end_headers()
      self.wfile.write(data)
  return Handler


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', type=Path, required=True)
  parser.add_argument('action', choices=('serve', 'pending', 'notify', 'notified', 'complete', 'report'))
  parser.add_argument('--claim', action='store_true')
  parser.add_argument('--summary-file', type=Path)
  parser.add_argument('--digest')
  parser.add_argument('--count', type=int)
  args = parser.parse_args()
  if args.config.stat().st_size > 8192:
    raise ValueError('invalid_pc_config')
  value = json.loads(args.config.read_text(encoding='utf-8'))
  inbox = Inbox(value['database'])
  if args.action == 'serve':
    if Path('/TICI').exists() or Path('/etc/nv_tegra_release').exists():
      raise RuntimeError('PC receiver only')
    key = bytes.fromhex(value['key'])
    if len(key) != 32:
      raise ValueError('invalid_key')
    if value.get('discord_bridge'):
      from tools.pv5_incidents.discord_report import Discord, notify_once
      discord = Discord(value)
      def notifications():
        next_send = 0.
        while True:
          try:
            if time.monotonic() >= next_send and notify_once(inbox, discord):
              next_send = time.monotonic() + 60.
          except Exception:
            next_send = time.monotonic() + 60.  # No secret/raw-log echo or retry storm.
          time.sleep(2.)
      threading.Thread(target=notifications, name='pc-receipt-notice', daemon=True).start()
    HTTPServer(('127.0.0.1', int(value['port'])), handler(Ingress(key, inbox))).serve_forever(poll_interval=.5)
  elif args.action == 'pending':
    print(json.dumps({'pending': inbox.pending(int(time.time()), args.claim)}))
  elif args.action == 'notify':
    print(json.dumps({'pending': inbox.notifications()}))
  elif args.action == 'notified':
    data = sys.stdin.buffer.read(4097)
    if len(data) > 4096:
      raise ValueError('too_large')
    inbox.mark_notified(json.loads(data))
  elif args.action == 'report':
    from tools.pv5_incidents.discord_report import Discord
    if not args.summary_file or args.summary_file.stat().st_size > 8192:
      raise ValueError('invalid_summary_file')
    summary = args.summary_file.read_text(encoding='utf-8')
    if not args.digest or not args.count or len(summary) > 1800 or '```' in summary:
      raise ValueError('invalid_summary')
    with inbox.open() as db:
      row = db.execute('SELECT total,report_count FROM incidents WHERE digest=?', (args.digest,)).fetchone()
      if not row or not 1 <= args.count <= row['total']:
        raise ValueError('unknown_incident')
      if row['report_count'] >= args.count:
        print(json.dumps({'already_reported': True})); return
    receipt = Discord(value).send(summary, [args.digest], 'analysis')
    if receipt['verified'] and receipt['purpose'] == 'analysis' and receipt['digests'] == [args.digest]:
      inbox.complete(args.digest, args.count, summary, receipt['message_id'], int(time.time()))
      print(json.dumps({'verified': True, 'message_id': receipt['message_id']}))
    else:
      raise ValueError('earlier_delivery_reconciled_retry_current_report')
  else:
    data = sys.stdin.buffer.read(16385)
    if len(data) > 16384:
      raise ValueError('too_large')
    row = json.loads(data)
    inbox.complete(row['digest'], row['count'], row['summary'], row['receipt'], int(time.time()))


if __name__ == '__main__':
  main()
