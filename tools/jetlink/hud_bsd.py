"""Fresh BSD stays visible; same-side turn requests flash independently of FPS."""
import math
import time

GRADIENT_ALPHA = 96


def flashing_sides(warnings, signals):
  return tuple(active is True and signal is True for active, signal in zip(warnings, signals, strict=True))


class WarningBrightness:
  """Boost throughout a warning, then use auto until the setting is changed."""
  def __init__(self):
    self.setting = None
    self.auto_after_warning = False

  def update(self, setting, warnings, signals):
    if setting != self.setting:
      self.setting = setting
      self.auto_after_warning = False
    if any(flashing_sides(warnings, signals)):
      self.auto_after_warning = True
      return 100
    return 0 if self.auto_after_warning else setting


class WarningPulse:
  """200ms on / 200ms off, preserving 2.5Hz at every admitted output rate."""
  def __init__(self):
    self.flashing = (False, False)
    self.alpha = (0, 0)
    self.phase = (0, 0)
    self.started = (None, None)

  def update(self, warnings, signals, now=None):
    now = time.monotonic() if now is None else now
    flashing = flashing_sides(warnings, signals)
    self.started = tuple(now if flash and (not previous or start is None or now < start) else start if flash else None
                         for flash, previous, start in zip(flashing, self.flashing, self.started, strict=True))
    self.phase = tuple(int(((now - start) % .4) * 10 + 1e-8) % 4 if start is not None else 0
                       for start in self.started)
    self.alpha = tuple(0 if not active else 255 if not flash or phase < 2 else 0
                       for active, flash, phase in zip(warnings, flashing, self.phase, strict=True))
    self.flashing = flashing
    return self.alpha


def warning_sides(state, snapshot, now):
  if snapshot is None:
    return False, False
  try:
    received, record = snapshot
    sent = record['sent']
    car_received = record['received']['carState']
    ages = (now - received, sent - car_received)
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (now, received, sent, car_received))
        or not all(0 <= age <= .3 for age in ages) or sum(ages) > .3
        or record['valid'].get('carState') is not True
        or record['alive'].get('carState') is not True):
      return False, False
    # carState already retains OEM BSD and ORs fresh optional ONNX results.
    # Neither a turn signal nor an available ONNX service gates this display.
    return state.left_blindspot is True, state.right_blindspot is True
  except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
    return False, False
