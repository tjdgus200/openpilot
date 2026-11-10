"""PV5 opt-in authority recovery, independent of model uncertainty.

This selects the existing CAN authority ceiling, never an angle or motor torque.
Legacy recovery continues independently for a bounded return when switched off.
"""
import math


DEFAULT_RELEASE_MS = 200
DEFAULT_RECOVERY_MS = 1500
RETURN_SECONDS = 1.5  # Preserve the existing bounded transition back to legacy when switched OFF.
FASTEST_RECOVERY_SECONDS = 1.0  # Existing selectable full-range rate ceiling, not a physical recovery guarantee.
MAX_RECOVERY_FACTOR = 1.5
ERROR_CONFIRM_SECONDS = 0.1
BOOST_SLEW_SECONDS = 0.2
TREND_FILTER_SECONDS = 0.15
TREND_LOOKAHEAD_SECONDS = 0.1
TREND_RATE_LIMIT = 10.0  # Tolerance widths/s; clip before filtering model-step noise.
TREND_DEADBAND = 0.1
TREND_ERROR_LIMIT = 0.5  # At most half a tolerance width of bounded look-ahead.
TIMING_SETTINGS = (
  ("Pv5SteerReleaseMs", 100, 500, 50, DEFAULT_RELEASE_MS),
  ("Pv5SteerRecoveryMs", 1000, 3000, 100, DEFAULT_RECOVERY_MS),
)


def _timing_ms(value, minimum, maximum, step, default):
  # Missing, corrupt, off-grid and out-of-range values use the documented default.
  return value if type(value) is int and minimum <= value <= maximum and (value - minimum) % step == 0 else default


def read_recovery_settings(params):
  values = []
  for name, minimum, maximum, step, default in TIMING_SETTINGS:
    try:
      # Typed get validates the entire integer string; get_int's std::stoi can
      # accept a numeric prefix in corrupt storage (for example "100junk").
      value = params.get(name)
    except (ValueError, TypeError, OverflowError, OSError):
      value = default
    values.append(_timing_ms(value, minimum, maximum, step, default))
  return tuple(values)


class SimpleSteeringRecovery:
  def __init__(self):
    self.reset()

  def reset(self):
    self.enabled = False
    self.returning = False
    self.cap = 0.0
    self.low_seconds = 0.0
    self.released = False
    self.last_time = None
    self.timing_locked = False
    self.release_seconds = DEFAULT_RELEASE_MS / 1000
    self.recovery_seconds = DEFAULT_RECOVERY_MS / 1000
    self._reset_boost()

  def _reset_boost(self):
    self.error_direction = 0
    self.error_seconds = 0.0
    self.recovery_boost = 0.0
    self.previous_errors = None
    self.error_rates = (0.0, 0.0)

  def _trend_demand(self, dt, errors, margin, demand):
    # Follow both raw-request AND limited-command errors. Differentiating only
    # their minimum could mistake a switch of the limiting cue for convergence.
    if self.previous_errors is not None:
      alpha = dt / (TREND_FILTER_SECONDS + dt)
      rates = [max(-TREND_RATE_LIMIT, min(TREND_RATE_LIMIT, (value - previous) / margin / dt))
               for value, previous in zip(errors, self.previous_errors, strict=True)]
      self.error_rates = tuple(old + alpha * (new - old) for old, new in zip(self.error_rates, rates, strict=True))
    self.previous_errors = errors
    a, b = self.error_rates
    trend = min(a, b) if min(a, b) > TREND_DEADBAND else max(a, b) if max(a, b) < -TREND_DEADBAND else 0.0
    correction = max(-TREND_ERROR_LIMIT, min(TREND_ERROR_LIMIT, trend * TREND_LOOKAHEAD_SECONDS))
    # Cannot qualify a sub-threshold error or change the base recovery/any limit.
    return min(1.0, max(0.0, demand + correction / 2.0)) if demand > 0 else 0.0

  def _update_boost(self, dt, target_error, command_error, speed, wheelbase, steer_ratio):
    # A raw request beyond the angle/rate limits cannot itself speed recovery.
    # Both errors must agree; missing geometry or a reversal discards old evidence.
    values = (target_error, command_error, speed, wheelbase, steer_ratio)
    if (not all(math.isfinite(value) for value in values) or speed <= 0 or
        wheelbase <= 0 or steer_ratio <= 0 or target_error * command_error <= 0):
      self._reset_boost()
      return
    direction = 1 if command_error > 0 else -1
    if direction != self.error_direction:
      self._reset_boost()
      self.error_direction = direction
    # Speed-normalized wheel-angle tolerance (heuristic, not measured lateral error).
    margin = min(5.0, max(1.0, math.degrees(math.atan(0.3 * wheelbase / max(speed * speed, 25.0))) * steer_ratio))
    error = min(abs(target_error), abs(command_error))
    demand = min(1.0, max(0.0, (error / margin - 1.0) / 2.0))
    trend_demand = self._trend_demand(dt, (abs(target_error), abs(command_error)), margin, demand)
    self.error_seconds = self.error_seconds + dt if demand > 0 else 0.0
    target = trend_demand if self.error_seconds + 1e-9 >= ERROR_CONFIRM_SECONDS else 0.0
    step = dt / BOOST_SLEW_SECONDS
    self.recovery_boost += max(-step, min(step, target - self.recovery_boost))

  def update(self, *, enabled, now, legacy, previous, minimum, maximum, active,
             valid, pressed, driver_ratio, pre_override_yield,
             release_ms=DEFAULT_RELEASE_MS, recovery_ms=DEFAULT_RECOVERY_MS,
             target_error=0.0, command_error=0.0, speed=0.0, wheelbase=0.0, steer_ratio=0.0):
    if not active:
      self.reset()
      return legacy
    if not enabled and not self.enabled and not self.returning:
      return legacy  # Default OFF retains the exact previous output.

    changed = enabled != self.enabled
    if changed:
      self.enabled = enabled
      self.returning = not enabled
      self.cap = previous
      self.low_seconds = 0.0
      self.released = False
      self.timing_locked = False
      self._reset_boost()
    dt = now - self.last_time if self.last_time is not None else 0.0
    self.last_time = now
    numbers = (now, dt, legacy, previous, minimum, maximum, driver_ratio, pre_override_yield)
    sane = (all(math.isfinite(v) for v in numbers) and 0 <= minimum < maximum
            and 0 <= legacy <= maximum and 0 <= previous <= maximum
            and driver_ratio >= 0 and 0 <= pre_override_yield <= 1)
    if not sane:
      self.low_seconds = 0.0
      self.released = False
      self.timing_locked = False
      self._reset_boost()
      self.cap = 0.0
      return 0.0
    self.cap = min(maximum, max(minimum, self.cap))
    if changed and valid:
      # Seed a live transition from the preceding actual selection, not the
      # background legacy ceiling. An existing driver yield still wins now.
      if pressed or pre_override_yield > 0:
        self.cap = min(self.cap, legacy)
      return self.cap
    if not valid or not 0 < dt <= .03:
      self.low_seconds = 0.0
      self.released = False
      self.timing_locked = False
      self._reset_boost()
      self.cap = min(self.cap, legacy, minimum)
      return self.cap

    if not self.returning and not self.timing_locked:
      # Freeze both settings from the first low-effort sample through recovery.
      # Editing a setting cannot shorten confirmation or change its base rate.
      self.release_seconds = _timing_ms(release_ms, *TIMING_SETTINGS[0][1:]) / 1000
      self.recovery_seconds = _timing_ms(recovery_ms, *TIMING_SETTINGS[1][1:]) / 1000
    rise = (maximum - minimum) * dt / (RETURN_SECONDS if self.returning else self.recovery_seconds)
    if self.returning:
      # A background legacy ramp cannot cause a sudden authority increase.
      if pressed or pre_override_yield > 0:
        fall = 20.0 if pressed else 10.0 * pre_override_yield
        self.cap = min(legacy, max(minimum, self.cap - fall))
      else:
        self.cap = min(legacy, self.cap + rise)
      if self.cap == legacy:
        self.returning = False
      return self.cap

    if pressed or pre_override_yield > 0:
      self.low_seconds = 0.0
      self.released = False
      self.timing_locked = False
      self._reset_boost()
      # Preserve the existing immediate override/pre-override decrement.
      fall = 20.0 if pressed else 10.0 * pre_override_yield
      self.cap = max(minimum, self.cap - fall)
    else:
      if not self.released:
        self.timing_locked = driver_ratio < .6
      self.low_seconds = self.low_seconds + dt if driver_ratio < .6 else 0.0
      self.released = self.released or self.low_seconds + 1e-9 >= self.release_seconds
      if driver_ratio < .6 and self.cap < maximum:
        self._update_boost(dt, target_error, command_error, speed, wheelbase, steer_ratio)
      else:
        self._reset_boost()
      if self.released:
        taper = min(1.0, max(0.0, (.8 - driver_ratio) / .2))
        factor = 1.0 + (min(MAX_RECOVERY_FACTOR, self.recovery_seconds / FASTEST_RECOVERY_SECONDS) - 1.0) * self.recovery_boost
        self.cap = min(maximum, self.cap + rise * taper * factor)
    return self.cap
