"""Bounded display cadence; never queue HUD frames or change model deadlines."""
import math
import select

PARAM = 'JetsonHudFps'
CAPABILITY = 'carrot_hud_fps_v1'
RATES = (10, 15, 20, 25, 30)
SHORT_NAMES = tuple(f'J92/{fps}' for fps in RATES)


def rate(value):
  try:
    result = int(value)
  except (TypeError, ValueError, OverflowError):
    return 10
  return result if not isinstance(value, bool) and result in RATES else 10


def selection(value):
  # Carrot Web select values are contiguous indices; labels show actual FPS.
  try:
    result = int(value)
  except (TypeError, ValueError, OverflowError):
    return 0
  return result if not isinstance(value, bool) and 0 <= result < len(RATES) else 0


def request_ready(connection, timeout):
  """Poll BEFORE reading a packet; never interrupt a partly read IPC frame."""
  try:
    return bool(select.select([connection], [], [], timeout)[0])
  except (TypeError, ValueError, AttributeError):
    return True  # Test/unsupported transports retain the original blocking read.


class IdleBudget:
  """Admit extra HUD only between model requests; back off after slow writes.

  This is best effort, not a hard real-time USB isolation guarantee. A single
  synchronous USB owner finishes each message, even if the device stalls.
  """
  def __init__(self):
    self.last_model = None
    self.period = .05
    self.pause_until = 0.
    self.send_budget = .002
    self.read_budget = 0.
    self.write_budget = .002
    self.media_budget = .002
    self.media_pause_until = 0.
    self.window_until = 0.
    self.media_window_until = 0.

  def model_started(self, now):
    if self.last_model is not None:
      interval = now - self.last_model
      if .025 <= interval <= .1:
        self.period = min(.05, interval)
    self.last_model = now

  def completed(self, now, exchange_s):
    if exchange_s >= .045:
      self.pause_until = max(self.pause_until, now + self.period)

  def open_window(self, now):
    # Snapshot access may exceed 2ms despite ample model headroom. Reserve
    # the measured USB write separately instead of discarding every snapshot.
    remaining = .012 if self.last_model is None or now - self.last_model > .5 else (
      self.last_model + self.period - now - .004 - self.write_budget)
    self.window_until = now + min(.012, max(0., remaining))
    self.media_window_until = now

  def open_media_window(self, now):
    self.media_window_until = min(self.window_until, now + .002)

  def within_window(self, now, *, writing=False, media=False):
    deadline = self.media_window_until if media else self.window_until
    return now < deadline and self.allowed(now, writing=writing, media=media)

  def allowed(self, now, *, writing=False, media=False):
    if now < (self.media_pause_until if media else self.pause_until):
      return False
    if self.last_model is None or now - self.last_model > .5:
      return True
    cost = self.media_budget if media else self.write_budget if writing else self.send_budget
    return self.last_model + self.period - now > .004 + cost

  def observe(self, now, metrics):
    def seconds(key):
      value = metrics.get(key, 0.) / 1000.
      return value if math.isfinite(value) and value >= 0 else 0.
    if 'display_snapshot_read_ms' in metrics or 'display_hud_send_ms' in metrics:
      read, write = seconds('display_snapshot_read_ms'), seconds('display_hud_send_ms')
      self.read_budget = max(min(.02, read), self.read_budget * .95)
      if metrics.get('display_hud_bytes', write > 0):
        self.write_budget = max(.002, min(.02, write), self.write_budget * .95)
      self.send_budget = self.read_budget + self.write_budget
      if read + write > .012 or (self.last_model is not None and now - self.last_model <= .5
                                and now >= self.last_model + self.period - .004):
        self.pause_until = max(self.pause_until, now + self.period)
    if 'display_media_send_ms' in metrics or 'display_media_read_ms' in metrics:
      duration = seconds('display_media_read_ms') + seconds('display_media_send_ms')
      if duration > 0:
        self.media_budget = max(.002, min(.02, duration), self.media_budget * .95)
        if duration > .002:
          self.media_pause_until = max(self.media_pause_until, now + self.period)


class FrameRate:
  """Constant-memory successful-frame counter, sampled by the existing heartbeat."""
  def __init__(self):
    self.started = None
    self.count = 0
    self.value = 0.

  def frame(self, now):
    if self.started is None or now < self.started:
      self.started, self.count, self.value = now, 0, 0.
      return
    self.count += 1

  def sample(self, now):
    if self.started is not None and now - self.started >= 1.:
      self.value = self.count / (now - self.started)
      self.started, self.count = now, 0
    return round(self.value, 1)
