"""One nonblocking, sampled, same-frame input slot per Jetson shadow session."""
import queue
import threading
import time

import numpy as np

from pv5_lane_review import classify, road_luma

PROCESS_SLOT = threading.Lock()
SAMPLE_NS = 250_000_000
RESULT_TTL_NS = 300_000_000
# If the current response already consumed most of a driving period, skip.
MAX_ADMISSION_NS = 35_000_000


class LaneReview:
  def __init__(self, predictor_factory, clock=time.monotonic_ns):
    self.factory, self.clock = predictor_factory, clock
    self.side, self.generation = 0, 0
    self.result = None
    self.next_sample = 0
    self.last_frame = None
    self.mail = queue.Queue(maxsize=1)
    self.slot = threading.Lock()
    self.stop = threading.Event()
    self.ready = threading.Event()
    self.thread = None
    self.failed = False

  def invalidate(self):
    # A worker can complete an earlier direction after an off/on transition.
    self.generation += 1
    self.side = 0
    self.result = self.last_frame = None
    self.next_sample = 0

  def set_side(self, value, frame):
    if self.side != value or (self.last_frame is not None and frame <= self.last_frame):
      self.invalidate()
    self.last_frame = frame
    self.side = value

  def latest(self):
    entry = self.result
    if entry is None:
      return None
    generation, stamp, row = entry
    return row if (generation == self.generation and 0 <= self.clock() - stamp < RESULT_TTL_NS) else None

  def offer(self, frame, stamp, warped, slices, output, valid):
    if not valid:
      self.invalidate()
      return
    if not self.side or self.last_frame != frame or self.stop.is_set() or self.failed:
      return
    try:
      logits = output[slices['lane_lines_prob']]
      if logits.shape != (8,):
        raise ValueError('Unsupported lane confidence layout')
      logit = float(logits[2 * self.side + 1])
      if not np.isfinite(logit) or logit < 0.:
        self.invalidate()
        return
    except (KeyError, IndexError, ValueError, TypeError):
      self.invalidate()
      return
    now = self.clock()
    if now < self.next_sample or not 0 <= now - stamp < MAX_ADMISSION_NS:
      return
    self.next_sample = now + SAMPLE_NS
    if self.thread is None:
      if PROCESS_SLOT.acquire(blocking=False):
        try:
          self.thread = threading.Thread(target=self._work, name='pv5-lane-review', daemon=True)
          self.thread.start()
        except Exception:
          self.failed = True
          PROCESS_SLOT.release()
      return
    if not self.ready.is_set() or not self.slot.acquire(blocking=False):
      return
    try:
      if warped.shape != (2, 6, 128, 256) or warped.dtype != np.uint8:
        raise ValueError('Unsupported road warp')
      rows = output[slices['lane_lines']]
      if rows.shape != (528,):
        raise ValueError('Unsupported lane geometry')
      # The first half is mu in the driving model MDN layout, as in Parser.parse_mdn.
      yz = rows[:264].reshape(4, 33, 2)[self.side].copy()
      probability = 1. / (1. + np.exp(-np.clip(logit, -11., 11.)))
      # The transport may reuse its storage after this callback. Own exactly
      # the selected road luma planes and boundary, never an engine output view.
      luma = bytes(memoryview(warped[0, :4]).cast('B'))
      self.mail.put_nowait((self.generation, self.side, frame, stamp, luma, yz, probability))
    except Exception:
      self.result = None
      self.slot.release()

  def close(self):
    self.stop.set()
    self.invalidate()
    try:
      self.mail.put_nowait(None)
    except queue.Full:
      pass

  def _work(self):
    predictor = None
    try:
      predictor = self.factory()
      self.ready.set()
      while not self.stop.is_set():
        job = self.mail.get()
        if job is None:
          break
        generation, side, frame, stamp, luma, yz, probability = job
        try:
          if generation != self.generation or self.clock() - stamp >= RESULT_TTL_NS:
            continue
          result = classify(road_luma(luma), side, yz, probability, predictor)
          if generation == self.generation and self.clock() - stamp < RESULT_TTL_NS:
            self.result = generation, stamp, (frame, side, *result)
        finally:
          self.slot.release()
    except Exception:
      self.failed = True
      self.result = None
    finally:
      self.ready.clear()
      try:
        if predictor is not None:
          predictor.close()
      finally:
        PROCESS_SLOT.release()
