"""Display-only turn-side warning from existing fresh vehicle BSD flags."""
import math


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
    return (state.left_signal is True and state.left_blindspot is True,
            state.right_signal is True and state.right_blindspot is True)
  except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
    return False, False
