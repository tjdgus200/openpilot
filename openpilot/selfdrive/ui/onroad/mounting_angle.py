"""PV5 display-only mounting estimate from the existing calibration subscriber."""
import math


def format_mounting_angles(rpy) -> str | None:
  try:
    if len(rpy) != 3:
      return None
    values = tuple(float(value) for value in rpy)
    if not all(math.isfinite(value) and abs(value) <= math.pi for value in values):
      return None
  except (TypeError, ValueError, OverflowError):
    return None

  def axis(value: float, positive: str, negative: str) -> str:
    degrees = round(abs(math.degrees(value)), 1)
    number = f"{degrees:.1f}".removesuffix(".0")
    # Rounded zero has no direction; do not show '-0' or a misleading arrow.
    arrow = "" if degrees == 0 else f"{positive if value > 0 else negative} "
    return f"{arrow}{number}°"

  # Same mounting convention as DeviceLayout's calibration description:
  # positive pitch points down, positive yaw points left. These ASCII
  # chevrons are already in the UI font atlas, unlike Unicode arrow glyphs.
  return f"{axis(values[1], 'v', '^')} | {axis(values[2], '<', '>')}"


class MountingAngleDisplay:
  def __init__(self):
    self.visible = False
    self.suppressed = True
    self.valid = False
    self.value = "-- | --"
    self._key = None

  def update(self, state) -> None:
    cp = getattr(state, "CP", None)
    self.visible = (getattr(cp, "brand", None) == "hyundai"
                    and getattr(cp, "carFingerprint", None) == "KIA_PV5"
                    and not getattr(cp, "notCar", True))
    if not self.visible:
      self._key = None
      self.valid = False
      self.value = "-- | --"
      self.suppressed = True
      return

    # Informational text yields to every existing alert and to missing alert
    # state. It never paints above a driver/vehicle warning.
    self.suppressed = True
    try:
      sm = state.sm
      if (sm.seen["selfdriveState"] and sm.alive["selfdriveState"] and sm.valid["selfdriveState"]
          and sm.recv_frame["selfdriveState"] >= state.started_frame):
        size = sm["selfdriveState"].alertSize
        self.suppressed = int(getattr(size, "raw", size)) != 0
    except (AttributeError, KeyError, TypeError, ValueError):
      pass

    try:
      sm = state.sm
      frame = sm.recv_frame["liveCalibration"]
      fresh = (sm.seen["liveCalibration"] and sm.alive["liveCalibration"]
               and sm.valid["liveCalibration"] and frame >= state.started_frame)
      key = (frame, bool(fresh), state.started_frame)
      if key == self._key:
        return
      calib = sm["liveCalibration"]
      value = format_mounting_angles(calib.rpyCalib) if fresh and str(calib.calStatus) == "calibrated" else None
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
      key, value = None, None
    self._key = key
    self.valid = value is not None
    self.value = value if self.valid else "-- | --"
