"""Authenticated resumable originals. PC-only; never deletes completed originals."""

from collections import OrderedDict
import contextlib
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time

from openpilot.system.pv5_incidents.archive_protocol import (
  PRIVACY,
  BATCH,
  CHUNK,
  MAX_MANIFEST,
  TOKEN,
  ZERO_CHAIN,
  advance,
  authenticate,
  encode,
  manifest,
)

GIB = 1024**3


class CapacityError(RuntimeError):
  pass


class OffsetError(RuntimeError):
  def __init__(self, offset):
    self.offset = offset


class Store:
  def __init__(self, root, key, quota=50 * GIB, reserve=10 * GIB, wake=lambda: None, overhead_per_segment=0, privacy=PRIVACY):
    self.root, self.key, self.quota, self.reserve, self.wake = Path(root), key, quota, reserve, wake
    self.overhead_per_segment = overhead_per_segment
    self.privacy = privacy
    self.external_usage = lambda: 0
    if len(key) != 32 or not self.root.is_absolute() or self.root.is_symlink():
      raise ValueError('invalid_archive_store')
    self.root.mkdir(parents=True, exist_ok=True)
    self.objects = self.root / 'objects'
    self.objects.mkdir(exist_ok=True)
    if self.objects.is_symlink():
      raise ValueError('invalid_archive_store')
    self.lock = threading.RLock()
    # Independent files can fsync/hash concurrently; each file still has one writer.
    self.file_locks = [threading.RLock() for _ in range(64)]
    self.database = self.root / 'archive.sqlite'
    if self.database.is_symlink():
      raise ValueError('invalid_archive_database')
    with self.db() as db:
      db.executescript('''
        CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY, manifest TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, name TEXT NOT NULL,
          size INTEGER NOT NULL, mtime INTEGER NOT NULL, offset INTEGER NOT NULL,
          chain TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS links(segment_id TEXT NOT NULL, file_id TEXT NOT NULL, PRIMARY KEY(segment_id,file_id));
        CREATE TABLE IF NOT EXISTS results(segment_id TEXT PRIMARY KEY, summary TEXT NOT NULL, stamp REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0);
      ''')

  @contextlib.contextmanager
  def db(self):
    if self.database.exists() and self.database.stat().st_size > 64 * 1024 * 1024:
      raise CapacityError('archive_metadata_budget')
    db = sqlite3.connect(self.database, timeout=1)
    try:
      db.row_factory = sqlite3.Row
      db.execute('PRAGMA journal_mode=DELETE')
      db.execute('PRAGMA max_page_count=16384')
      with db:
        yield db
    finally:
      db.close()

  def _path(self, token, name, partial=False):
    if not TOKEN.fullmatch(token):
      raise ValueError('invalid_token')
    folder = self.objects / token
    if folder.is_symlink():
      raise ValueError('unsafe_archive_path')
    folder.mkdir(exist_ok=True)
    path = folder / (name + ('.part' if partial else ''))
    if path.is_symlink() or path.resolve().parent != folder.resolve():
      raise ValueError('unsafe_archive_path')
    return path

  @staticmethod
  def _status(row):
    return {'file_id': row['id'], 'offset': row['offset'], 'chain': row['chain'], 'complete': row['status'] == 'complete'}

  def begin(self, data):
    if not 0 < len(data) <= MAX_MANIFEST:
      raise ValueError('invalid_manifest')
    token, value = manifest(self.key, json.loads(data), self.privacy)
    with self.lock, self.db() as db:
      count = db.execute('SELECT count(*) FROM files').fetchone()[0]
      reserved = self.reserved_size(db)
      new_segment = int(not db.execute('SELECT 1 FROM segments WHERE id=?', (token,)).fetchone())
      reserved += (db.execute('SELECT count(*) FROM segments').fetchone()[0] + new_segment) * self.overhead_per_segment
      reserved += min(self.quota // 32, 64 * 1024**2) if self.overhead_per_segment else 0
      if count >= 32768 or db.execute('SELECT count(*) FROM segments').fetchone()[0] >= 16384:
        raise CapacityError('archive_catalog_full')
      new = [entry for entry in value['files'] if not db.execute('SELECT 1 FROM files WHERE id=?', (entry['file_id'],)).fetchone()]
      extra = sum(entry['size'] for entry in new)
      if reserved + extra + self.external_usage() > self.quota or shutil.disk_usage(self.root).free < self.reserve + extra:
        raise CapacityError('archive_storage_budget')
      db.execute('INSERT OR IGNORE INTO segments VALUES(?,?,?,?,NULL)', (token, encode(value).decode(), 'uploading', time.time()))
      for entry in value['files']:
        db.execute(
          'INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?,?)', (entry['file_id'], entry['name'], entry['size'], entry['mtime_ns'], 0, ZERO_CHAIN, 'partial')
        )
        row = db.execute('SELECT * FROM files WHERE id=?', (entry['file_id'],)).fetchone()
        if (row['name'], row['size'], row['mtime']) != (entry['name'], entry['size'], entry['mtime_ns']):
          raise ValueError('file_identity_conflict')
        if row['status'] == 'complete':
          stored = self._path(row['id'], row['name'])
          if not self.stored(row, stored):
            raise ValueError('stored_data_missing')
        db.execute('INSERT OR IGNORE INTO links VALUES(?,?)', (token, entry['file_id']))
      rows = db.execute('SELECT files.* FROM files JOIN links ON files.id=links.file_id WHERE segment_id=? ORDER BY files.id', (token,)).fetchall()
      complete = all(row['status'] == 'complete' for row in rows)
      if complete:
        db.execute("UPDATE segments SET status='ready' WHERE id=? AND status='uploading'", (token,))
      result = {'segment_id': token, 'files': [self._status(row) for row in rows], 'complete': complete, 'max_batch_bytes': BATCH}
    if complete:
      self.wake()
    return result

  def stored(self, row, path):
    return path.is_file() and path.stat().st_size == row['size']

  def reserved_size(self, db):
    return db.execute('SELECT coalesce(sum(size),0) FROM files').fetchone()[0]

  def reserved_bytes(self):
    with self.lock, self.db() as db:
      return self.reserved_size(db) + self.overhead_per_segment * db.execute('SELECT count(*) FROM segments').fetchone()[0]

  def chunk(self, token, offset, body):
    return self._append(token, offset, body, CHUNK)

  def batch(self, token, offset, body):
    return self._append(token, offset, body, BATCH)

  def _file_lock(self, token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
      raise ValueError('invalid_token')
    return self.file_locks[int(token[:8], 16) % len(self.file_locks)]

  def _append(self, token, offset, body, maximum):
    if not isinstance(body, bytes) or not 0 < len(body) <= maximum:
      raise ValueError('invalid_chunk')
    # Hold the per-file lock across I/O and its durable database checkpoint.
    # Do not hold the global metadata lock across fsync or file hashing.
    with self._file_lock(token):
      with self.lock, self.db() as db:
        row = db.execute('SELECT * FROM files WHERE id=?', (token,)).fetchone()
        if row is None or type(offset) is not int or not 0 <= offset < row['size']:
          raise ValueError('unknown_file_or_offset')
        if len(body) != min(maximum, row['size'] - offset) or offset % CHUNK:
          raise ValueError('invalid_chunk_size')
        if offset > row['offset']:
          raise OffsetError(row['offset'])
        row = dict(row)
        path = self._path(token, row['name'], row['status'] != 'complete')
      if offset < row['offset'] or row['status'] == 'complete':
        if not path.exists():
          raise ValueError('stored_data_missing')
        with path.open('rb') as stream:
          stream.seek(offset)
          if stream.read(len(body)) != body:
            raise ValueError('chunk_conflict')
        return self._status(row)
      # Allow for all four ingress writers checking free space concurrently.
      if shutil.disk_usage(self.root).free < self.reserve + 4 * len(body):
        raise CapacityError('archive_free_space_floor')
      mode = 'r+b' if path.exists() else 'x+b'
      with path.open(mode) as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() < row['offset']:
          raise ValueError('partial_data_missing')
        stream.truncate(row['offset'])
        stream.seek(row['offset'])
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
      chain = row['chain']
      for start in range(0, len(body), CHUNK):
        chain = advance(chain, body[start : start + CHUNK])
      with self.lock, self.db() as db:
        current = db.execute('SELECT offset,chain,status FROM files WHERE id=?', (token,)).fetchone()
        if current is None or tuple(current) != (row['offset'], row['chain'], row['status']):
          raise ValueError('concurrent_file_writer')
        db.execute('UPDATE files SET offset=?,chain=? WHERE id=?', (offset + len(body), chain, token))
      return {'file_id': token, 'offset': offset + len(body), 'chain': chain, 'complete': False}

  def seal(self, token, data):
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != {'size', 'chain'}:
      raise ValueError('invalid_seal')
    with self._file_lock(token):
      with self.lock, self.db() as db:
        row = db.execute('SELECT * FROM files WHERE id=?', (token,)).fetchone()
        if row is None or value != {'size': row['size'], 'chain': row['chain']} or row['offset'] != row['size']:
          raise ValueError('incomplete_or_invalid_seal')
        row = dict(row)
        path = self._path(token, row['name'], partial=True)
        final = self._path(token, row['name'])
      if row['status'] != 'complete':
        source = path if path.exists() else final
        chain, size, digest = ZERO_CHAIN, 0, hashlib.sha256()
        with source.open('rb') as stream:
          while block := stream.read(CHUNK):
            chain = advance(chain, block)
            digest.update(block)
            size += len(block)
        if size != row['size'] or chain != row['chain']:
          raise ValueError('archive_integrity_mismatch')
        if path.exists():
          os.replace(path, final)
      with self.lock, self.db() as db:
        db.execute("UPDATE files SET status='complete' WHERE id=?", (token,))
        db.execute("""UPDATE segments SET status='ready' WHERE status='uploading' AND
                 NOT EXISTS(SELECT 1 FROM links JOIN files ON files.id=links.file_id
                            WHERE links.segment_id=segments.id AND files.status!='complete')""")
        result = self._status(dict(row, status='complete'))
    self.wake()
    return result

  def claim(self):
    with self.lock, self.db() as db:
      row = db.execute("SELECT * FROM segments WHERE status='ready' ORDER BY created LIMIT 1").fetchone()
      early = row is None
      if early:
        row = db.execute("""SELECT segments.* FROM segments WHERE status='uploading'
          AND NOT EXISTS(SELECT 1 FROM results WHERE results.segment_id=segments.id)
          AND EXISTS(SELECT 1 FROM links JOIN files ON files.id=links.file_id
            WHERE links.segment_id=segments.id AND files.name IN ('rlog.zst','rlog.bz2','rlog') AND files.status='complete')
          ORDER BY created LIMIT 1""").fetchone()
      if row is None:
        return None
      db.execute('UPDATE segments SET status=? WHERE id=?', ('screening_log' if early else 'analyzing', row['id']))
      files = db.execute('SELECT files.* FROM files JOIN links ON files.id=links.file_id WHERE segment_id=?', (row['id'],)).fetchall()
      return {
        'segment_id': row['id'],
        'manifest': json.loads(row['manifest']),
        'files': {value['name']: str(self._path(value['id'], value['name'])) for value in files
                  if not early or (value['status']=='complete' and value['name'] in ('rlog.zst','rlog.bz2','rlog'))},
      }

  def analyzed(self, token, summary):
    data = encode(summary)
    if len(data) > 8192:
      raise ValueError('analysis_result_budget')
    with self.lock, self.db() as db:
      db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?,0)', (token, data.decode(), time.time()))
      row = db.execute('SELECT status FROM segments WHERE id=?', (token,)).fetchone()
      status = 'analyzed'
      if row is not None and row['status'] == 'screening_log':
        missing = db.execute("SELECT 1 FROM files JOIN links ON files.id=links.file_id WHERE segment_id=? AND files.status!='complete' LIMIT 1", (token,)).fetchone()
        status = 'uploading' if missing else 'ready'
      db.execute('UPDATE segments SET status=?,error=NULL WHERE id=?', (status, token))

  def failed(self, token, reason):
    if not re.fullmatch(r'[A-Za-z_]{1,64}', reason):
      reason = 'analysis_failed'
    with self.lock, self.db() as db:
      db.execute("UPDATE segments SET status='failed',error=? WHERE id=?", (reason, token))

  def recover(self):
    with self.lock, self.db() as db:
      db.execute("UPDATE segments SET status='ready' WHERE status='analyzing'")
      db.execute("UPDATE segments SET status='uploading' WHERE status='screening_log'")
      db.execute("""UPDATE segments SET status='ready' WHERE status='uploading' AND
        NOT EXISTS(SELECT 1 FROM links JOIN files ON files.id=links.file_id
          WHERE links.segment_id=segments.id AND files.status!='complete')""")


class Ingress:
  def __init__(self, store, raw_store=None):
    self.raw_store = raw_store
    self.slots = threading.BoundedSemaphore(4)
    self.store, self.nonces, self.lock = store, OrderedDict(), threading.Lock()

  def ingest(self, path, values, body, now):
    with self.slots:
      return self._ingest(path, values, body, now)

  def _ingest(self, path, values, body, now):
    if not authenticate(self.store.key, path, values, body, now):
      raise PermissionError('unauthorized')
    nonce = values['X-PV5-Nonce']
    with self.lock:
      while self.nonces and (len(self.nonces) >= 512 or next(iter(self.nonces.values())) < now - 300):
        self.nonces.popitem(last=False)
      if nonce in self.nonces:
        raise PermissionError('replay')
      self.nonces[nonce] = now
    store = self.store
    if path.startswith('/v1/archive/raw/'):
      if self.raw_store is None:
        raise PermissionError('raw_receipt_not_enabled')
      store = self.raw_store
      path = path.replace('/v1/archive/raw/', '/v1/archive/', 1)
    if path == '/v1/archive/manifest':
      return store.begin(body)
    match = re.fullmatch(r'/v1/archive/chunk/([0-9a-f]{64})/([0-9]{1,10})', path)
    if match:
      return store.chunk(match[1], int(match[2]), body)
    match = re.fullmatch(r'/v1/archive/batch/([0-9a-f]{64})/([0-9]{1,10})', path)
    if match:
      return store.batch(match[1], int(match[2]), body)
    match = re.fullmatch(r'/v1/archive/seal/([0-9a-f]{64})', path)
    if match:
      return store.seal(match[1], body)
    raise ValueError('invalid_archive_endpoint')


class BoundedHTTPServer(ThreadingHTTPServer):
  daemon_threads = True

  def __init__(self, *args, **kwargs):
    # Four persistent uploader lanes must not occupy every incident/remote slot.
    # Ingress independently limits archive writes to four.
    self.slots = threading.BoundedSemaphore(8)
    super().__init__(*args, **kwargs)

  def process_request(self, request, address):
    if not self.slots.acquire(blocking=False):
      self.shutdown_request(request)
      return
    try:
      super().process_request(request, address)
    except BaseException:
      self.slots.release()
      raise

  def process_request_thread(self, request, address):
    try:
      super().process_request_thread(request, address)
    finally:
      self.slots.release()


def handler(base, ingress):
  class Handler(base):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
      if not self.path.startswith('/v1/archive/'):
        return super().do_POST()
      limit = MAX_MANIFEST
      if re.fullmatch(r'/v1/archive/(?:raw/)?chunk/[0-9a-f]{64}/[0-9]{1,10}', self.path):
        limit = CHUNK
      elif re.fullmatch(r'/v1/archive/(?:raw/)?batch/[0-9a-f]{64}/[0-9]{1,10}', self.path):
        limit = BATCH
      length = self.headers.get('Content-Length', '')
      if self.headers.get('Transfer-Encoding') or not re.fullmatch(r'[0-9]{1,7}', length) or not 0 < int(length) <= limit:
        self.send_error(413)
        return
      try:
        body = self.rfile.read(int(length))
        if len(body) != int(length):
          raise ValueError('truncated_body')
        value, status = ingress.ingest(self.path, self.headers, body, time.time()), 200
      except PermissionError:
        value, status = {'error': 'unauthorized'}, 403
      except OffsetError as error:
        value, status = {'error': 'offset', 'offset': error.offset}, 409
      except CapacityError:
        value, status = {'error': 'storage_budget'}, 507
      except (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error, OSError):
        value, status = {'error': 'invalid_archive_request'}, 422
      data = encode(value)
      self.send_response(status)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(data)))
      self.end_headers()
      self.wfile.write(data)

  return Handler
