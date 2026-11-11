"""Display-only missing model-output frame counts; bounded storage, no I/O.

Count gaps between published camera frame IDs at their capture timestamps.
Report a one-second observation window at 1Hz, not a filtered percent
or HUD/USB packet loss. A gap is attributed when the next output observes it.
Startup, sequence resets and unobservable windows are unknown, never zero.
"""
from collections import deque

SECOND = 1_000_000_000


class ModelFrameDropWindow:
  def __init__(self):
    self.frame = self.stamp = self.end = None
    self.count = 0
    self.completed = None
    self.gaps = deque()

  def update(self, frame, stamp):
    if type(frame) is not int or not 0 <= frame <= 0xffffffff or type(stamp) is not int or not 0 < stamp < 2**64:
      self.__init__()
      return None
    if (self.frame is None or frame <= self.frame or stamp <= self.stamp or stamp - self.stamp >= SECOND or
        stamp >= self.end + SECOND):
      self.frame, self.stamp, self.end = frame, stamp, stamp + SECOND
      self.count, self.completed = 0, None
      self.gaps.clear()
      return None
    while self.gaps and stamp - self.gaps[0][0] >= SECOND:
      self.count -= self.gaps.popleft()[1]
    missing = frame - self.frame - 1
    if missing:
      # Normal camera rate is 20Hz. Malformed/high-rate input must not grow
      # storage without limit or silently drop old loss evidence.
      if len(self.gaps) >= 64:
        self.__init__()
        return None
      self.gaps.append((stamp, missing))
      self.count += missing
    if stamp >= self.end:
      self.completed = self.count, stamp
      self.end = stamp + SECOND
    self.frame, self.stamp = frame, stamp
    return self.completed
