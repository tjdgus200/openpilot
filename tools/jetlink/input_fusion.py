"""Park-only, bounded receive-complete -> TX-ready input-path observations.

No extra inference, automatic ranking/selection, file history or GPU worker.
Statistics are computed at1Hz outside the request thread; two6000-row windows.
"""
from collections import deque
import json
import os
import threading
import time

PARAM = 'JetsonInputFusion'
LIMIT = 6000
WARMUP = 15.


def selection(value):
  try:
    return 0 if not isinstance(value, bool) and int(value) == 0 else 1
  except (ValueError, TypeError, OverflowError):
    return 1


def gate(sm, now):
  try:
    names = ('carState', 'carControl', 'selfdriveState')
    return (all(sm.valid[n] and sm.alive[n] and 0 <= now - sm.recv_time[n] <= .5 for n in names)
            and str(sm['carState'].gearShifter) == 'park' and sm['carState'].standstill
            and sm['carState'].vEgo == 0 and not sm['carControl'].enabled and not sm['selfdriveState'].active)
  except (KeyError, TypeError, AttributeError):
    return False


def distribution(values):
  values = sorted(values)
  at = .99 * (len(values) - 1)
  i = int(at)
  return {'mean_ms': sum(values) / len(values),
          'p99_ms': values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (at - i),
          'max_ms': values[-1], 'over50': sum(v > 50 for v in values)}


class Comparison:
  def __init__(self, clock=time.monotonic, *, worker=True):
    self.clock = clock
    self.park_until = -1.
    self.requested, self.effective = 1, 0
    self.loaded = None
    self.generation = 0
    self.rows = (deque(maxlen=LIMIT), deque(maxlen=LIMIT))
    self.since = clock()
    self.was_parked = False
    self.reason = 'waiting'
    self.fps = None
    self.failed = 0
    self.value = {}
    self.stop = threading.Event()
    self.thread = None
    self.next_observe = 0.
    if worker:
      self.thread = threading.Thread(target=self._run, name='input-path-stats', daemon=True)
      self.thread.start()

  def observe(self, payload, received):
    now = self.clock()
    if now < self.next_observe or not 0 <= now - received <= 1.:
      return
    self.next_observe = now + .2
    try:
      data = json.loads(bytes(payload))
      control = data['input_fusion']
      if (not isinstance(control, dict) or type(control.get('mode')) is not int
          or control['mode'] not in (0, 1) or type(control.get('parked')) is not bool):
        return
      self.requested = control['mode']
      self.park_until = received + 1. if control['parked'] else -1.
      fps = control.get('hud_fps')
      if type(fps) is int and fps in (10, 15, 20, 25, 30) and fps != self.fps:
        self.fps = fps
        self.clear(now)
    except (ValueError, KeyError, TypeError):
      pass

  def clear(self, now):
    self.generation += 1
    for row in self.rows:
      row.clear()
    self.failed = 0
    self.since = now
    self.value = {}

  def before(self, loaded):
    now = self.clock()
    if loaded is not self.loaded:
      self.loaded = loaded
      self.clear(now)
    fusion = getattr(loaded.engine, 'image_fusion', None)
    supported = fusion is not None and fusion.qualified
    self.reason = 'ready' if supported else getattr(loaded.engine, 'image_fusion_reason', 'unsupported')
    parked = now <= self.park_until
    if supported and parked:
      fusion.select(self.requested)
    actual = fusion.active if supported else 0
    if actual != self.effective or parked != self.was_parked:
      self.generation += 1
      self.value = {}
      if parked:
        self.rows[actual].clear()
      self.effective, self.was_parked, self.since = actual, parked, now

  def record(self, received_ns, ready_ns):
    now = self.clock()
    if (self.reason != 'ready' or now > self.park_until or not self.was_parked
        or received_ns / 1e9 < self.since + WARMUP
        or not 0 < ready_ns - received_ns <= 10_000_000_000):
      return
    self.rows[self.effective].append((ready_ns / 1e9, (ready_ns - received_ns) / 1e6))

  def report(self):
    now, generation = self.clock(), self.generation
    rows = []
    for mode, source in enumerate(self.rows):
      samples = source.copy()  # bounded atomic snapshot, no hot-path lock/sort
      if samples:
        rows.append({'id': mode, 'n': len(samples), 'seconds': samples[-1][0] - samples[0][0],
                     **distribution([r[1] for r in samples])})
    parked = now <= self.park_until
    state = ('unsupported' if self.reason not in ('ready', 'waiting') else
             'waiting' if self.loaded is None or not parked else
             'warmup' if now - self.since < WARMUP else 'measuring')
    if state == 'measuring' and any(r['id'] == self.effective and r['n'] >= 1000 and r['seconds'] >= 60 for r in rows):
      state = 'ready'
    value = {'version': 1, 'scope': 'rx_complete_to_tx_ready', 'state': state, 'requested': self.requested,
             'effective': self.effective, 'change_allowed': parked and self.reason == 'ready',
             'reason': self.reason, 'rows': rows, 'failed': min(self.failed, 1_000_000), 'hud_fps': self.fps}
    if generation == self.generation:
      self.value = value
    return value

  def _run(self):
    if hasattr(os, 'nice'):
      try:
        os.nice(19)
      except OSError:
        pass
    while not self.stop.wait(1.):
      try:
        self.report()
      except (ArithmeticError, RuntimeError, TypeError, ValueError):
        self.value = {}

  def close(self):
    self.stop.set()
    if self.thread is not None:
      self.thread.join(timeout=1.2)
