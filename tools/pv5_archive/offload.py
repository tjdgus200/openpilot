"""Private PC quarantine. Only sanitized durable copies enter the analysis store."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from openpilot.system.pv5_incidents.archive_protocol import PC_RAW, PRIVACY, MAX_FILE, BATCH, TOKEN, encode, file_id
from tools.pv5_archive.store import Store
from tools.pv5_archive.service import atomic


class RawStore(Store):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, privacy=PC_RAW, **kwargs)
    with self.db() as db:
      db.execute('CREATE TABLE IF NOT EXISTS filtered(id TEXT PRIMARY KEY, entry TEXT NOT NULL)')

  def reserved_size(self, db):
    return db.execute('SELECT coalesce(sum(size),0) FROM files WHERE NOT EXISTS '
                      '(SELECT 1 FROM filtered WHERE filtered.id=files.id)').fetchone()[0]

  def stored(self, row, path):
    with self.db() as db:
      receipt = db.execute('SELECT 1 FROM filtered WHERE id=?', (row['id'],)).fetchone()
    return receipt is not None or super().stored(row, path)

  def claim(self):
    with self.lock, self.db() as db:
      row = db.execute("SELECT * FROM segments WHERE status='ready' ORDER BY created LIMIT 1").fetchone()
      if row is None:
        return None
      db.execute("UPDATE segments SET status='preparing' WHERE id=?", (row['id'],))
      return {'segment_id': row['id'], 'manifest': json.loads(row['manifest'])}

  def recover(self):
    with self.lock, self.db() as db:
      db.execute("UPDATE segments SET status='ready' WHERE status='preparing'")
      rows = db.execute('SELECT files.* FROM files JOIN filtered ON files.id=filtered.id').fetchall()
    # These rows have durable successful privacy + clean-store receipts only.
    for row in rows:
      with self._file_lock(row['id']):
        self._path(row['id'], row['name']).unlink(missing_ok=True)
        self._path(row['id'], row['name'], True).unlink(missing_ok=True)
    super().recover()


def connect_budget(raw, clean):
  # Shared metadata lock makes combined quota reservations atomic, not file IO.
  raw.lock = clean.lock
  scratch = 4 * MAX_FILE
  raw.external_usage = lambda: clean.reserved_bytes() + scratch
  clean.external_usage = lambda: raw.reserved_bytes() + scratch


class PreparationQueue:
  def __init__(self, raw, clean, python, code, notify=lambda: None, workers=4, ffmpeg=None, ffprobe=None):
    if type(workers) is not int or not 1 <= workers <= 4:
      raise ValueError('invalid_preparation_parallelism')
    self.raw, self.clean, self.python, self.code, self.notify = raw, clean, str(python), Path(code), notify
    self.workers, self.ffmpeg, self.ffprobe = workers, ffmpeg, ffprobe
    self.event, self.stopping = threading.Event(), False
    self.folder = raw.root / 'preparation'
    if self.folder.is_symlink():
      raise ValueError('unsafe_preparation_directory')
    self.folder.mkdir(exist_ok=True)
    connect_budget(raw, clean)
    raw.wake = self.wake

  def wake(self):
    self.event.set()

  def execute(self, request, result):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    child = subprocess.Popen([self.python, '-B', '-X', 'utf8', '-m', 'tools.pv5_archive.prepare',
                              '--job', str(request), '--result', str(result)], cwd=self.code,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != 'nt')
    try:
      if child.wait(timeout=210):
        raise ValueError('pc_preprocessing_failed')
    finally:
      if child.poll() is None:
        if os.name == 'nt':
          child.kill()  # Child owns a kill-on-close Job containing its media helper.
        else:
          os.killpg(child.pid, signal.SIGKILL)
        child.wait()
      if os.name != 'nt':
        try:
          os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
          pass
    if result.is_symlink() or not result.is_file() or result.stat().st_size > 4096:
      raise ValueError('pc_preprocessing_result')
    return json.loads(result.read_bytes())

  def step(self):
    job = self.raw.claim()
    if job is None:
      return False
    token, original = job['segment_id'], job['manifest']
    if not TOKEN.fullmatch(token):
      raise ValueError('invalid_preparation_identity')
    temporary, entries, sources, mapped = [], [], {}, {}
    locks = {int(entry['file_id'][:8], 16) % len(self.raw.file_locks) for entry in original['files']}
    with contextlib.ExitStack() as stack:
      for index in sorted(locks):
        stack.enter_context(self.raw.file_locks[index])
      try:
        remaining = MAX_FILE
        for original_entry in original['files']:
          raw_id = original_entry['file_id']
          with self.raw.lock, self.raw.db() as db:
            saved = db.execute('SELECT entry FROM filtered WHERE id=?', (raw_id,)).fetchone()
          if saved:
            entry = json.loads(saved['entry'])
            source = self.clean._path(entry['file_id'], entry['name'])
            if not source.is_file() or source.stat().st_size != entry['size']:
              raise ValueError('filtered_receipt_data_missing')
          else:
            source_raw = self.raw._path(raw_id, original_entry['name'])
            source = self.folder / (token + '.' + raw_id + '.part')
            request, result = source.with_suffix('.job.json'), source.with_suffix('.result.json')
            temporary.extend((source, request, result))
            for path in (source, request, result):
              if path.is_symlink():
                raise ValueError('unsafe_preparation_file')
              path.unlink(missing_ok=True)  # Only this job's generated, uncommitted scratch.
            atomic(request, {'source': str(source_raw), 'output': str(source), 'reserve': self.raw.reserve,
                             'maximum': remaining, 'ffmpeg': self.ffmpeg, 'ffprobe': self.ffprobe})
            audit = self.execute(request, result)
            entry = {'name': audit['name'], 'size': source.stat().st_size, 'mtime_ns': original_entry['mtime_ns']}
            if entry['size'] != audit['size'] or not 0 < entry['size'] <= remaining:
              raise ValueError('preparation_output_budget')
            remaining -= entry['size']
            entry['file_id'] = file_id(self.clean.key, original['segment'], entry['name'], entry['size'], entry['mtime_ns'])
          entries.append(entry)
          sources[entry['file_id']] = source
          mapped[raw_id] = entry
        value = dict(original, privacy=PRIVACY, files=entries)
        begin = self.clean.begin(encode(value))
        states = {row['file_id']: row for row in begin['files']}
        for entry in entries:
          state = states[entry['file_id']]
          if state['complete']:
            continue
          with sources[entry['file_id']].open('rb') as stream:
            stream.seek(state['offset'])
            while block := stream.read(BATCH):
              state = self.clean.batch(entry['file_id'], state['offset'], block)
          self.clean.seal(entry['file_id'], encode({'size': entry['size'], 'chain': state['chain']}))
        # Commit all clean durable receipts before removing temporary originals.
        with self.raw.lock, self.raw.db() as db:
          for raw_id, entry in mapped.items():
            db.execute('INSERT OR REPLACE INTO filtered VALUES(?,?)', (raw_id, encode(entry).decode()))
          db.execute("UPDATE segments SET status='prepared',error=NULL WHERE id=?", (token,))
        for entry in original['files']:
          self.raw._path(entry['file_id'], entry['name']).unlink(missing_ok=True)
          self.raw._path(entry['file_id'], entry['name'], True).unlink(missing_ok=True)
      except Exception:
        # Preserve failed raw originals in private quarantine for explicit recovery.
        # No raw path/content enters analysis/dispatch; ordinary claims exclude failed.
        summary = {'version': 1, 'status': 'analysis_failed', 'failure': 'pc_privacy_preprocessing_failed',
                   'candidates': [], 'missing_services': [], 'visual_semantics_evaluated': False}
        self.clean.analyzed(token, summary)
        self.raw.failed(token, 'pc_privacy_preprocessing_failed')
        atomic(self.folder / (token + '.failure.json'), summary)
        self.notify()
      finally:
        for path in temporary:
          path.unlink(missing_ok=True)
    return True

  def run(self):
    self.raw.recover()
    self.wake()
    def worker():
      first = True
      while not self.stopping:
        if not first:
          self.event.wait()
        first = False
        self.event.clear()
        try:
          while not self.stopping and self.step():
            pass
        except Exception as error:
          atomic(self.folder / ('queue-' + threading.current_thread().name + '.json'),
                 {'status': 'needs_attention', 'error_type': type(error).__name__})
    threads = [threading.Thread(target=worker, name='prepare-' + str(i), daemon=True) for i in range(self.workers)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()


def enable(store, archive, code, notify=lambda: None):
  """Explicit owner policy only; legacy ingress remains default."""
  if archive.get('pc_preprocess_temporary_raw') is not True:
    return None
  raw = RawStore(store.root / 'quarantine', store.key, quota=store.quota, reserve=store.reserve)
  queue = PreparationQueue(raw, store, archive['analysis_python'], code, notify,
                           workers=4, ffmpeg=archive.get('pc_ffmpeg'), ffprobe=archive.get('pc_ffprobe'))
  threading.Thread(target=queue.run, name='pc-privacy-preparation', daemon=True).start()
  return raw
