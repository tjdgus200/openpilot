"""Fresh, display-only OEM marking codes for the actual PV5."""

import time


MAX_DISPLAY_AGE_NS = 250_000_000
UNKNOWN_CODES = (-1, -1)


def _display_code(code: int) -> int:
  if type(code) is int and 0 <= code < 40 and code % 10 in (0, 1, 5):
    return code
  return -1


def pv5_lane_display_codes(sm, *, source_event_mono_ns=None, now_ns=None) -> tuple[int, int] | None:
  """Return left/right codes, or None for vehicles outside the PV5 display path.

  Jetson passes the original carState event timestamp. Its SubMaster separately
  translates that event's time to the receiver clock for the transport age.
  """
  try:
    car_params = sm['carParams']
  except (KeyError, AttributeError, TypeError):
    return None
  if getattr(car_params, 'carFingerprint', '') != 'KIA_PV5' or getattr(car_params, 'notCar', True):
    return None

  try:
    state = sm['carState']
    marking = state.pv5LaneMarking
    stamp = marking.displayMonoTime
    event_ns = sm.logMonoTime['carState']
    if (not sm.valid['carParams'] or not sm.valid['carState'] or not state.canValid or state.canTimeout
        or stamp <= 0 or event_ns <= 0):
      return UNKNOWN_CODES
    source_ns = event_ns if source_event_mono_ns is None else source_event_mono_ns
    source_age = source_ns - stamp
    elapsed = (time.monotonic_ns() if now_ns is None else now_ns) - event_ns
    if source_age < 0 or elapsed < 0 or source_age + elapsed > MAX_DISPLAY_AGE_NS:
      return UNKNOWN_CODES
    return _display_code(marking.leftDisplayCode), _display_code(marking.rightDisplayCode)
  except (KeyError, AttributeError, TypeError, ValueError):
    return UNKNOWN_CODES
