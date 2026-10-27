"""Bounded, Jetson-only observations of actual GPU/server inference samples.

Never average overlapping ten-second P99s. Keep at most 6000 current-trial
samples; persist eight small summaries in RAM so display changes retain results.
"""
from collections import deque
import json
import math
import os
from pathlib import Path
import time

PATH = Path('/dev/shm/carrot-hud-quality.json')
MAX_BYTES = 16384
MAX_SAMPLES = 6000
WARMUP = 15.
MIN_SECONDS = 60.
MIN_SAMPLES = 1000


def number(value, lower, upper):
  return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


def valid_row(row):
  return (isinstance(row, dict) and type(row.get('id')) is int and 0 <= row['id'] < 8
          and type(row.get('n')) is int and 1 <= row['n'] <= MAX_SAMPLES
          and number(row.get('seconds'), 0, 3600)
          and all(number(row.get(k), 0, 10000) for k in ('mean_ms', 'p99_ms', 'max_ms', 'server_p99_ms'))
          and all(type(row.get(k)) is int and 0 <= row[k] <= row['n'] for k in ('over50', 'server_over50'))
          and row['max_ms'] >= max(row['mean_ms'], row['p99_ms']))


def p99(values):
  values = sorted(values)
  at = .99 * (len(values) - 1)
  index = int(at)
  return values[index] + (values[min(index + 1, len(values) - 1)] - values[index]) * (at - index)


class Comparison:
  def __init__(self, requested, effective, error='', *, path=PATH, clock=time.perf_counter, kind='codec'):
    self.kind = kind
    if kind == 'fps-q92' and path == PATH:
      path = PATH.with_name('carrot-hud-fps-q92.json')
    self.requested, self.effective = requested, effective
    self.error, self.path, self.clock = error[:160], path, clock
    self.started = clock()
    self.last_stamp = -1.
    self.samples = deque(maxlen=MAX_SAMPLES)
    self.session = None
    self.rows = {}
    self.cached_session = None
    try:
      with path.open('rb') as source:
        raw = source.read(MAX_BYTES + 1)
      if len(raw) <= MAX_BYTES:
        saved = json.loads(raw)
        session = saved.get('session')
        if (saved.get('version') == 1 and saved.get('kind', 'codec') == kind
            and isinstance(session, str) and len(session) <= 128):
          self.cached_session = session
          for row in saved.get('rows', [])[:8]:
            if valid_row(row) and row['id'] != requested and (kind != 'fps-q92' or row['id'] < 5):
              self.rows[row['id']] = row
    except (OSError, ValueError, TypeError, AttributeError):
      pass

  def update(self, timing):
    now = self.clock()
    if now < self.started:
      self.started = now
      self.rows.clear()
      self.samples.clear()
      self.last_stamp = -1.
    state = 'fallback' if self.error else 'waiting'
    if not self.error and isinstance(timing, dict):
      session, batch = timing.get('sample_session'), timing.get('samples')
      if isinstance(session, str) and len(session) <= 128 and isinstance(batch, list) and len(batch) <= 64:
        if session != self.session:
          if session != self.cached_session:
            self.rows.clear()
          self.session = session
          self.cached_session = None
          self.samples.clear()
          self.last_stamp = -1.
          self.started = now
        state = 'warmup' if now - self.started < WARMUP else 'measuring'
        for row in batch:
          if (not isinstance(row, (list, tuple)) or len(row) != 3
              or not number(row[0], 0, now) or row[0] <= self.last_stamp
              or not all(number(v, 0, 10_000_000) for v in row[1:])):
            continue
          self.last_stamp = row[0]
          if self.samples and row[0] - self.samples[-1][0] > 3.:
            self.samples.clear()
            self.rows.pop(self.requested, None)
            self.started = row[0]
            state = 'warmup'
          if row[0] >= self.started + WARMUP:
            self.samples.append(row)
        if self.samples:
          span = self.samples[-1][0] - self.samples[0][0]
          gpu = [row[2] / 1000. for row in self.samples]
          server = [row[1] / 1000. for row in self.samples]
          self.rows[self.requested] = {'id': self.requested, 'n': len(gpu), 'seconds': span,
            'mean_ms': sum(gpu) / len(gpu), 'p99_ms': p99(gpu), 'max_ms': max(gpu),
            'over50': sum(v > 50 for v in gpu), 'server_p99_ms': p99(server),
            'server_over50': sum(v > 50 for v in server)}
          if len(gpu) >= MIN_SAMPLES and span >= MIN_SECONDS:
            state = 'ready'
    rows = [self.rows[key] for key in sorted(self.rows)]
    ready = [row for row in rows if row['n'] >= MIN_SAMPLES and row['seconds'] >= MIN_SECONDS]
    best = (min(ready, key=lambda r: (r['p99_ms'], r['over50'] / r['n'], r['server_p99_ms']))
            if self.session is not None and state in ('measuring', 'ready') and len(ready) >= 2 else None)
    value = {'version': 1, 'kind': self.kind, 'session': self.session or self.cached_session, 'active': self.requested,
             'effective': self.effective, 'state': state, 'error': self.error,
             'best': best['id'] if best else None, 'rows': rows}
    # One bounded RAM write per existing low-frequency stats sample, no thread.
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) <= MAX_BYTES:
      try:
        temporary = self.path.with_suffix('.tmp')
        temporary.write_bytes(raw)
        os.replace(temporary, self.path)
      except OSError:
        pass
    return value
