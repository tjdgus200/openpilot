"""PC-only completion-event queue. No periodic AI or device requests."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading

from openpilot.system.pv5_incidents.archive_protocol import TOKEN, encode
from tools.pv5_archive.event_diagnostics import is_event_clip


def atomic(path, value):
  temporary = path.with_suffix('.tmp')
  temporary.write_bytes(encode(value))
  temporary.replace(path)


class AnalysisQueue:
  def __init__(self, store, python, code, notify=lambda: None, workers=1):
    self.store, self.python, self.code, self.notify = store, str(python), Path(code), notify
    if type(workers) is not int or not 1 <= workers <= 8:
      raise ValueError('invalid_analysis_parallelism')
    self.workers = workers
    self.event = threading.Event()
    self.cache = store.root / 'screen-cache'
    if self.cache.is_symlink():
      raise ValueError('unsafe_screen_cache')
    self.cache.mkdir(exist_ok=True)
    self.identity = hashlib.sha256(b''.join((self.code / 'tools/pv5_archive' / name).read_bytes() for name in ('analyze.py', 'screen.py', 'service.py', 'event_diagnostics.py'))).hexdigest()
    self.cache_locks, self.cache_guard = {}, threading.Lock()
    self.folder = store.root / 'analysis'
    if self.folder.is_symlink():
      raise ValueError('unsafe_analysis_directory')
    self.folder.mkdir(exist_ok=True)
    self.stopping = False

  def wake(self):
    self.event.set()

  def step(self):
    job = self.store.claim()
    if job is None:
      return False
    token = job['segment_id']
    if not TOKEN.fullmatch(token):
      raise ValueError('invalid_analysis_identity')
    request, output = self.folder / (token + '.job.json'), self.folder / (token + '.json')
    logs = sorted((value for value in job['manifest']['files'] if value['name'] in ('rlog.zst','rlog.bz2','rlog')), key=lambda value:value['name'])
    cache_id = logs[0]['file_id'] if logs else token
    if not TOKEN.fullmatch(cache_id):
      raise ValueError('invalid_cache_identity')
    with self.cache_guard:
      cache_lock = self.cache_locks.setdefault(cache_id, threading.Lock())
    job['screen_cache'] = str(self.cache / (cache_id + '.' + self.identity[:16] + '.json'))
    atomic(request, job)
    try:
      flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
      cache_lock.acquire()
      try:
        result = subprocess.run(
        [self.python, '-X', 'utf8', '-m', 'tools.pv5_archive.service', '--job', str(request), '--output', str(output)],
        cwd=self.code,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=210,
        creationflags=flags,
      )
      finally:
        cache_lock.release()
      if result.returncode or not output.is_file() or output.stat().st_size > 1024**2:
        raise ValueError('bounded_analysis_failed')
      summary = json.loads(output.read_bytes())
      summary.pop('samples', None)
      summary.pop('event_markers', None)
      self.store.analyzed(token, summary)
    except (ValueError, OSError, subprocess.SubprocessError):
      summary = {
        'version': 1,
        'status': 'analysis_failed',
        **({'event_clip': True} if is_event_clip(job['manifest']) else {}),
        'candidates': [],
        'missing_services': [],
        'failure': 'bounded_analysis_failed',
        'visual_semantics_evaluated': False,
      }
      atomic(output, summary)
      self.store.analyzed(token, summary)
    finally:
      request.unlink(missing_ok=True)
    summary_file = self.folder / (token + '.summary.json')
    atomic(summary_file, summary)
    self.notify()
    return True

  def run(self):
    self.store.recover()
    self.wake()  # Once: recover durable work after PC restart.
    def worker():
      first_run = True
      while not self.stopping:
        if not first_run:
          self.event.wait()
        first_run = False
        self.event.clear()
        try:
          while not self.stopping and self.step():
            pass
        except Exception as error:
          atomic(self.folder / ('queue-status-' + threading.current_thread().name + '.json'),
                 {'status': 'needs_attention', 'error_type': type(error).__name__})
    threads = [threading.Thread(target=worker, name='screen-' + str(i), daemon=True) for i in range(self.workers)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()


def main():
  from tools.pv5_archive.limits import apply

  apply()
  parser = argparse.ArgumentParser()
  parser.add_argument('--job', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  if args.job.stat().st_size > 8192 or args.job.parent != args.output.parent or args.output.is_symlink():
    raise ValueError('invalid_analysis_job')
  job = json.loads(args.job.read_bytes())
  token = job['segment_id']
  if not TOKEN.fullmatch(token) or args.job.name != token + '.job.json' or args.output.name != token + '.json':
    raise ValueError('invalid_analysis_job')
  from tools.pv5_archive.analyze import analyze
  from tools.pv5_archive.schema import full_schema

  event_manifest = job.get('manifest', {})
  event_clip = is_event_clip(event_manifest)
  event_anchor = event_manifest['files'][0]['mtime_ns'] if event_clip else None
  rlogs = [path for name, path in job['files'].items() if name in ('rlog.zst', 'rlog.bz2', 'rlog')]
  if not rlogs:
    result = {'version': 1, 'status': 'insufficient_data', 'candidates': [], 'missing_services': ['rlog'], 'visual_semantics_evaluated': False}
  else:
    cache = Path(job['screen_cache']) if job.get('screen_cache') else None
    if cache is not None and (cache.parent != args.job.parent.parent / 'screen-cache' or cache.is_symlink()):
      raise ValueError('invalid_screen_cache_path')
    cache_reused = cache is not None and cache.exists()
    if cache_reused:
      if cache.stat().st_size > 1024**2:
        raise ValueError('screen_cache_budget')
      result = json.loads(cache.read_bytes())
    else:
      with full_schema() as schema:
        result = analyze(rlogs[0], schema, event_anchor=event_anchor)
      if not event_clip:
        from tools.pv5_archive.screen import select_review
        result = select_review(result)
        if not result['vehicle_pv5'] or result['missing_services'] or result['source_commit'] is None:
          result['status'] = 'insufficient_data'
      if cache is not None:
        atomic(cache, result)
  result['data_screen_cache_reused'] = bool(rlogs) and cache_reused
  result['road_video_present'] = any(name.startswith(('fcamera.', 'ecamera.', 'qcamera.')) for name in job['files'])
  if not result['road_video_present'] and not event_clip:
    result['awaiting_media'] = bool(rlogs)
    if result.get('status') not in ('review_candidates', 'no_threshold_candidate'):
      result['status'] = 'insufficient_data'
  atomic(args.output, result)


if __name__ == '__main__':
  main()
