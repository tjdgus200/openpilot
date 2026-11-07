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
    self.window_until = 0.

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
    # One bounded slice after polling IPC, never a burst/catch-up queue.
    self.window_until = now + .002

  def within_window(self, now):
    return now < self.window_until and self.allowed(now)

  def allowed(self, now):
    if now < self.pause_until:
      return False
    if self.last_model is None or now - self.last_model > .5:
      return True
    return self.last_model + self.period - now > .004 + self.send_budget

  def observe(self, now, metrics):
    duration = sum(metrics.get(key, 0.) for key in (
      'display_snapshot_read_ms', 'display_hud_send_ms',
      'display_media_read_ms', 'display_media_send_ms')) / 1000.
    if math.isfinite(duration) and duration >= 0:
      self.send_budget = max(.002, min(.02, duration), self.send_budget * .95)
      if duration > .002:
        # Skip a model period, not two seconds of vehicle telemetry. A slow
        # display write must not force the receiver past its 500ms data TTL.
        # The normal headroom/IPC gates still apply after this retry delay.
        self.pause_until = max(self.pause_until, now + self.period)


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
