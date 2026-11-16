"""PV5-only experimental stock-deceleration arbitration, default OFF.

No standalone engagement, CAN subscriptions, persistent writes or background work.
These calibrations are PC trial values, not a validated emergency-braking system.
"""

from dataclasses import dataclass
import math

from opendbc.car.hyundai.values import CAR, HyundaiFlags

SOURCE_MAX_AGE_NS = 100_000_000
CLEAR_TIME_NS = 200_000_000
ENTRY_ACCEL = -2.0
DECEL_JERK = 5.0  # also used by the existing CAN jerk-limit fields
ENTRY_BLEND_START = -1.5  # qualified OEM pre-entry band; never delays raw <= ENTRY_ACCEL
RELEASE_JERK = 1.0
MIN_ACCEL, MAX_ACCEL = -4.0, 2.5
MOVING_SPEED = 0.1


def eligible(cp):
  flags = HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC
  return bool(cp.brand == "hyundai" and cp.carFingerprint == CAR.KIA_PV5 and not cp.notCar and cp.openpilotLongitudinalControl and (cp.flags & flags) == flags)


def fresh(stamp, now):
  return isinstance(stamp, int) and 0 < stamp <= now and now - stamp <= SOURCE_MAX_AGE_NS


@dataclass(frozen=True)
class Observation:
  stamp: int = 0
  raw: float = 0.0
  value: float = 0.0
  valid: bool = False


class CameraSccSource:
  """One accepted original camera message, never a TX echo or cached template."""

  def __init__(self, checksum):
    self.checksum = checksum
    self.reset()

  def reset(self):
    self.stamp = 0
    self.counter = None
    self.payload = None
    self.observation = Observation()

  def update(self, parser, camera_bus, now):
    if parser is None or parser.bus != camera_bus:
      self.reset()
      return Observation()
    stamp = parser.ts_nanos.get("SCC_CONTROL", {}).get("COUNTER", 0)
    payload = parser.dat.get(0x1A0, b"")
    state = parser.message_states.get(0x1A0)
    if state is None or state.last_invalid_nanos >= stamp or state.counter_fail:
      self.reset()
      return Observation()
    if not fresh(stamp, now) or len(payload) != 32:
      self.reset()
      return Observation()
    if stamp == self.stamp:
      return self.observation if payload == self.payload else Observation()
    if stamp < self.stamp:
      self.reset()
      return Observation()
    if int.from_bytes(payload[:2], "little") != self.checksum(0x1A0, None, bytearray(payload)):
      self.reset()
      return Observation()
    values = parser.vl["SCC_CONTROL"]
    try:
      counter = int(values["COUNTER"])
      raw, value = float(values["aReqRaw"]), float(values["aReqValue"])
      mode_ok = values["MainMode_ACC"] == 1 and values["ACCMode"] == 1 and values["SysFailState"] == 0
    except (KeyError, TypeError, ValueError, OverflowError):
      self.reset()
      return Observation()
    # One card tick may contain multiple accepted camera frames. Validate the
    # whole existing parser batch, rather than treating its last counter as a
    # missing wire frame. Rejected frames still break qualification.
    counters = parser.vl_all["SCC_CONTROL"]["COUNTER"] or [counter]
    previous = self.counter
    consecutive = previous is not None and state.last_invalid_nanos <= self.stamp
    for item in counters:
      current = int(item)
      consecutive = consecutive and (current - previous) % 256 == 1
      previous = current
    consecutive = consecutive and previous == counter
    self.stamp, self.counter, self.payload = stamp, counter, bytes(payload)
    valid = consecutive and mode_ok and math.isfinite(raw) and math.isfinite(value)
    self.observation = Observation(stamp, raw, value, valid)
    return self.observation


def _driver_inputs_valid(cs, now, pt_bus):
  """Shared moving-state checks; neither pedals nor engagement are admitted here."""
  out = cs.out
  wheels = out.wheelSpeeds
  speeds = (out.vEgo, out.vEgoRaw, wheels.fl, wheels.fr, wheels.rl, wheels.rr)
  parser = getattr(cs, "cp", None)
  if parser is None or parser.bus != pt_bus:
    return False
  tcs_stamp = parser.ts_nanos.get("TCS", {}).get("DriverBraking", 0)
  if not fresh(tcs_stamp, now):
    return False
  tcs = parser.message_states.get(0x175)
  if tcs is None or tcs.last_invalid_nanos >= tcs_stamp or tcs.counter_fail:
    return False
  if (
    not all(math.isfinite(v) for v in speeds)
    or not out.canValid
    or out.canTimeout
    or str(out.gearShifter) != "drive"
    or out.parkingBrake
    or out.brakeHoldActive
    or cs.paddle_button_prev != 0
  ):
    return False
  braking = out.brakePressed or bool(parser.vl["TCS"]["DriverBraking"])
  return not braking


def packet_allowed(cc, cs, now, pt_bus):
  """Longitudinal actuation admission; retain the existing stationary hold."""
  out = cs.out
  wheels = out.wheelSpeeds
  speeds = (out.vEgo, out.vEgoRaw, wheels.fl, wheels.fr, wheels.rl, wheels.rr)
  stationary_hold = (
    cs.softHoldActive > 0 and all(math.isfinite(v) for v in speeds) and max(abs(v) for v in speeds) <= MOVING_SPEED and out.cruiseState.available
  )
  if stationary_hold:
    # This experiment must not replace the existing stopped/soft-hold policy.
    # Its original interlocks remain in create_acc_control_scc2/apply_canfd_stopping.
    return True
  return bool(_driver_inputs_valid(cs, now, pt_bus) and cc.enabled and cc.longActive
              and not out.gasPressed and not cc.cruiseControl.override)


def gas_override_allowed(cc, cs, now, pt_bus):
  """Keep cruise's driver-override mode, never authorize an acceleration request."""
  return bool(cc.enabled and not cc.longActive and cs.out.gasPressed and cc.cruiseControl.override
              and not getattr(cc.cruiseControl, "cancel", False)
              and math.isfinite(cs.out.aEgo) and cs.softHoldActive == 0 and cs.out.carrotCruise == 0
              and _driver_inputs_valid(cs, now, pt_bus))


@dataclass(frozen=True)
class Decision:
  accel: float
  active: bool = False
  source_stamp: int = 0


class StockDecelAssist:
  def __init__(self):
    self.reset()

  def reset(self):
    self.phase = "normal"
    self.last_accel = None
    self.last_now = 0
    self.clear_since = 0

  def update(self, accel, source, now, admitted):
    if not math.isfinite(accel):
      self.reset()
      return Decision(0.0)
    if not admitted or not source.valid or not fresh(source.stamp, now) or not math.isfinite(source.raw) or not math.isfinite(source.value):
      self.reset()
      return Decision(accel)
    if self.last_now and (now <= self.last_now or now - self.last_now > SOURCE_MAX_AGE_NS):
      self.reset()
      return Decision(accel)
    dt = (now - self.last_now) * 1e-9 if self.last_now else 0.01
    self.last_now = now
    if self.phase == "normal" and source.raw <= ENTRY_ACCEL:
      self.phase = "stock"
    if self.phase == "normal":
      # Blend BEFORE the hard entry, not after an urgent request. A sudden
      # raw <= ENTRY_ACCEL still enters stock above on this very call.
      # Smoothstep joins both band endpoints without changing the stronger
      # comma request. Weak/positive OEM requests cannot create new braking.
      weight = max(0.0, min(1.0, (ENTRY_BLEND_START - source.raw) / (ENTRY_BLEND_START - ENTRY_ACCEL)))
      weight = weight * weight * (3.0 - 2.0 * weight)
      requested = accel + weight * (min(accel, source.raw) - accel)
      if weight == 0.0 and self.last_accel is None:
        return Decision(accel)
      previous = accel if self.last_accel is None else self.last_accel
      # Only pre-entry preparation is slew-limited. Never limit a stronger
      # base request, and never run this branch for an urgent raw request.
      requested = min(accel, max(requested, previous - DECEL_JERK * min(dt, 0.05)))
      selected = max(MIN_ACCEL, min(MAX_ACCEL, requested))
      if self.last_accel is not None:
        selected = min(selected, self.last_accel + RELEASE_JERK * min(dt, 0.05))
      if selected >= accel - 1e-9:
        self.last_accel = None
        return Decision(accel)
      self.last_accel = selected
      return Decision(selected, True, source.stamp)
    if source.raw < 0.0 or source.value < 0.0:
      self.clear_since = 0
      self.phase = "stock"
      requested = min(accel, source.raw if source.raw < 0.0 else source.value)
    else:
      if not self.clear_since:
        self.clear_since = source.stamp
      if source.stamp - self.clear_since >= CLEAR_TIME_NS:
        self.phase = "handover"
      requested = min(accel, 0.0) if self.phase == "stock" else accel
    selected = max(MIN_ACCEL, min(MAX_ACCEL, requested))
    if self.last_accel is not None:
      selected = min(selected, self.last_accel + RELEASE_JERK * min(dt, 0.05))
    self.last_accel = selected
    if self.phase == "handover" and selected >= accel - 1e-9:
      self.reset()
      return Decision(accel)
    return Decision(selected, True, source.stamp)


def feedback_active(cp, output, output_stamp, now):
  return bool(
    eligible(cp)
    and output.pv5SccDecelEnabled
    and output.pv5SccDecelActive
    and fresh(output_stamp, now)
    and fresh(output.pv5SccDecelSourceMonoTime, now)
    and math.isfinite(output.accel)
    and MIN_ACCEL <= output.accel <= MAX_ACCEL
  )


def warning_active(cp, cs, output, output_stamp, now):
  return bool(
    feedback_active(cp, output, output_stamp, now)
    and output.accel < 0.0
    and cs.canValid
    and not cs.canTimeout
    and not cs.brakePressed
    and not cs.gasPressed
    and str(cs.gearShifter) == "drive"
    and not cs.parkingBrake
    and not cs.brakeHoldActive
  )
