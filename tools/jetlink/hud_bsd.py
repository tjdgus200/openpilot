"""Fresh BSD stays visible; a same-side turn request flashes the 10 Hz HUD."""
import math

GRADIENT_ALPHA = 96


class WarningPulse:
  """One phase step per rendered frame: nominal 5 Hz at the existing 10 fps.

  Frame locking avoids time-phase aliasing at this output-rate limit. Dropped
  output frames can slow the visible cadence; no extra USB frames are produced.
  """
  def __init__(self):
    self.flashing = (False, False)
    self.alpha = (0, 0)

  def update(self, warnings, signals):
    flashing = tuple(active and signal is True for active, signal in zip(warnings, signals, strict=True))
    self.alpha = tuple(0 if not active else
                       255 if not flash or not previous else 255 - alpha
                       for active, flash, previous, alpha in
                       zip(warnings, flashing, self.flashing, self.alpha, strict=True))
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
