"""PV5 opt-in authority recovery, independent of model uncertainty.

This selects the existing CAN authority ceiling, never an angle or motor torque.
Legacy recovery continues independently for a bounded return when switched off.
"""
import math


RECOVERY_SECONDS = 1.2  # PV5 opt-in full-range recovery; low-force confirmation stays unchanged.
RETURN_SECONDS = 1.5  # Preserve the existing bounded transition back to legacy when switched OFF.
RELEASE_SECONDS = 0.2  # Existing low-driver-force confirmation.


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

  def update(self, *, enabled, now, legacy, previous, minimum, maximum, active,
             valid, pressed, driver_ratio, pre_override_yield):
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
    dt = now - self.last_time if self.last_time is not None else 0.0
    self.last_time = now
    numbers = (now, dt, legacy, previous, minimum, maximum, driver_ratio, pre_override_yield)
    sane = (all(math.isfinite(v) for v in numbers) and 0 <= minimum < maximum
            and 0 <= legacy <= maximum and 0 <= previous <= maximum
            and driver_ratio >= 0 and 0 <= pre_override_yield <= 1)
    if not sane:
      self.low_seconds = 0.0
      self.released = False
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
      self.cap = min(self.cap, legacy, minimum)
      return self.cap

    rise = (maximum - minimum) * dt / (RETURN_SECONDS if self.returning else RECOVERY_SECONDS)
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
      # Preserve the existing immediate override/pre-override decrement.
      fall = 20.0 if pressed else 10.0 * pre_override_yield
      self.cap = max(minimum, self.cap - fall)
    else:
      self.low_seconds = self.low_seconds + dt if driver_ratio < .6 else 0.0
      self.released = self.released or self.low_seconds + 1e-9 >= RELEASE_SECONDS
      if self.released:
        taper = min(1.0, max(0.0, (.8 - driver_ratio) / .2))
        self.cap = min(maximum, self.cap + rise * taper)
    return self.cap
