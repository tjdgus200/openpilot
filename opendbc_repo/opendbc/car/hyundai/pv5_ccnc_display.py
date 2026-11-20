"""PV5 OEM cluster display state. Does not provide inputs to vehicle control."""

from opendbc.car.hyundai.values import CAR, HyundaiFlags

PLAN_MAX_AGE_NS = 200_000_000
_RED_CONFIRM_NS = 300_000_000
_GREEN_TIME_NS = 2_000_000_000
_MAX_DISPLAY_GAP_NS = 200_000_000


def supported(cp) -> bool:
  flags = HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC
  return (cp.brand == "hyundai" and cp.carFingerprint == CAR.KIA_PV5 and not cp.notCar
          and cp.openpilotLongitudinalControl and (cp.flags & flags) == flags)


def plan_signal_state(plan) -> int:
  if not plan.signalDetectionSuspended and not plan.fcw:
    if plan.trafficState == 1 and plan.shouldStop and plan.signalObservationColor not in (2, 3):
      return 3 if plan.signalObservationColor == 1 else 1
    if plan.trafficState == 2 and plan.signalObservationColor == 2:
      return 2
  return 0


class Pv5CcncDisplay:
  def __init__(self):
    self.show_assist = False
    self.traffic_mode = 0
    self._last_update = 0
    self._red_since = None
    self._observed_red_since = None
    self._red_confirmed = False
    self._green_until = 0

  def configure(self, params):
    self.show_assist = params.get_int("Pv5CcncClusterDisplay") == 1
    mode = params.get_int("Pv5CcncTrafficBackground")
    if self.traffic_mode != mode:
      self._reset_signal()
    self.traffic_mode = mode if mode in (1, 2) else 0

  def _reset_signal(self):
    self._red_since = None
    self._observed_red_since = None
    self._red_confirmed = False
    self._green_until = 0

  def traffic_background(self, hud, now_ns: int, allowed: bool, stationary: bool) -> int:
    gap = now_ns - self._last_update
    self._last_update = now_ns
    if not allowed or not stationary or self.traffic_mode == 0 or not 0 < gap <= _MAX_DISPLAY_GAP_NS:
      self._reset_signal()
      return 0

    state = hud.pv5CcncTrafficState
    if state in (1, 3):
      self._green_until = 0
      if self._red_since is None:
        self._red_since = now_ns
      if state == 1:
        self._observed_red_since = None
      elif self._observed_red_since is None:
        self._observed_red_since = now_ns
      self._red_confirmed = (self._observed_red_since is not None
                             and now_ns - self._observed_red_since >= _RED_CONFIRM_NS)
      return 5 if now_ns - self._red_since >= _RED_CONFIRM_NS else 0

    if state != 2 or self.traffic_mode != 2:
      self._reset_signal()
      return 0
    if self._red_confirmed:
      self._green_until = now_ns + _GREEN_TIME_NS
    self._red_since = None
    self._observed_red_since = None
    self._red_confirmed = False
    return 8 if now_ns < self._green_until else 0
