"""PC-only completion-event queue. No periodic AI or device requests."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import threading

from openpilot.system.pv5_incidents.archive_protocol import TOKEN, encode


def atomic(path, value):
  temporary = path.with_suffix('.tmp')
  temporary.write_bytes(encode(value))
  temporary.replace(path)


class AnalysisQueue:
  def __init__(self, store, python, code, notify=lambda: None):
    self.store, self.python, self.code, self.notify = store, str(python), Path(code), notify
    self.event = threading.Event()
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
    atomic(request, job)
    try:
      flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
      result = subprocess.run(
        [self.python, '-X', 'utf8', '-m', 'tools.pv5_archive.service', '--job', str(request), '--output', str(output)],
        cwd=self.code,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=210,
        creationflags=flags,
      )
      if result.returncode or not output.is_file() or output.stat().st_size > 1024**2:
        raise ValueError('bounded_analysis_failed')
      summary = json.loads(output.read_bytes())
      summary.pop('samples', None)
      self.store.analyzed(token, summary)
    except (ValueError, OSError, subprocess.SubprocessError):
      summary = {
        'version': 1,
        'status': 'analysis_failed',
        'candidates': [],
        'missing_services': [],
        'failure': 'bounded_analysis_failed',
        'visual_semantics_evaluated': False,
      }
      atomic(output, summary)
      self.store.analyzed(token, summary)
    finally:
      request.unlink(missing_ok=True)
    self.notify()
    return True

  def run(self):
    self.store.recover()
    self.wake()  # Once: recover durable work after PC restart.
    while not self.stopping:
      self.event.wait()
      self.event.clear()
      try:
        while not self.stopping and self.step():
          pass
      except Exception as error:
        atomic(self.folder / 'queue-status.json', {'status': 'needs_attention', 'error_type': type(error).__name__})


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

  rlogs = [path for name, path in job['files'].items() if name in ('rlog.zst', 'rlog.bz2', 'rlog')]
  if not rlogs:
    result = {'version': 1, 'status': 'insufficient_data', 'candidates': [], 'missing_services': ['rlog'], 'visual_semantics_evaluated': False}
  else:
    with full_schema() as schema:
      result = analyze(rlogs[0], schema)
    if not result['vehicle_pv5'] or result['missing_services'] or result['source_commit'] is None:
      result['status'] = 'insufficient_data'
  result['road_video_present'] = any(name.startswith(('fcamera.', 'ecamera.', 'qcamera.')) for name in job['files'])
  if not result['road_video_present']:
    result['status'] = 'insufficient_data'
  atomic(args.output, result)


if __name__ == '__main__':
  main()
