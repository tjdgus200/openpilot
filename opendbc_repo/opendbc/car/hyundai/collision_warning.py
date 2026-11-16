"""PV5 display-only OFF-cruise warning; conservative PC trial thresholds.

This is not an AEB detector. No controls, CAN writes, engagement changes or
assumption that the OEM will brake. Existing FCW and SCC assistance are separate.
"""
from dataclasses import dataclass
import math

from opendbc.car.hyundai.scc_decel import CameraSccSource, fresh


@dataclass(frozen=True)
class RiskInputs:
  version: int = 1
  source_valid: bool = False
  stamp: int = 0
  raw: float = 0.0
  distance: float = 0.0
  relative_speed: float = 0.0
  target_valid: bool = False
  inactive_clear: bool = False


class SccRiskSource:
  """Independent observer of the existing camera parser; never a TX template.

  Reuses transport/mode qualification without changing the assist observer or
  its state. The parser owns freshness/CRC/counter checks; no new subscription.
  """
  def __init__(self, checksum, camera_bus):
    self.source = CameraSccSource(checksum)
    self.camera_bus = camera_bus

  def update(self, parser, now):
    observation = self.source.update(parser, self.camera_bus, now)
    if not observation.transport_valid:
      return RiskInputs()
    values = parser.vl['SCC_CONTROL']
    inactive_clear = (values.get('ACCMode') == 0 and values.get('MainMode_ACC') in (0, 1)
                      and values.get('SysFailState') == 0 and -3.0 < observation.raw <= 2.5)
    try:
      distance = float(values.get('ACC_ObjDist', 0.0))
      relative_speed = float(values.get('ACC_ObjRelSpd', 0.0))
    except (TypeError, ValueError, OverflowError):
      return RiskInputs(source_valid=observation.valid, stamp=observation.stamp, raw=observation.raw, inactive_clear=inactive_clear)
    target_valid = (values.get('HUD_LEAD_INFO') in (1, 2) and math.isfinite(distance)
                    and math.isfinite(relative_speed) and 0.0 < distance < 150.0
                    and -16.4 <= relative_speed <= 34.7)
    return RiskInputs(source_valid=observation.valid, stamp=observation.stamp, raw=observation.raw,
                      distance=distance if target_valid else 0.0,
                      relative_speed=relative_speed if target_valid else 0.0, target_valid=target_valid, inactive_clear=inactive_clear)


def write_inputs(cs, inputs):
  """Only new display metadata; do not rewrite any existing CarState field."""
  out = cs.pv5CollisionWarning
  out.version = inputs.version
  out.sourceValid = inputs.source_valid
  out.sourceMonoTime = inputs.stamp
  out.raw = inputs.raw
  out.distance = inputs.distance
  out.relativeSpeed = inputs.relative_speed
  out.targetValid = inputs.target_valid
  out.inactiveClear = inputs.inactive_clear


@dataclass(frozen=True)
class WarningCue:
  level: int = 0
  audible: bool = False


class OffCruiseWarning:
  """Immediate qualified warning; one sound per risk episode and escalation.

  No dwell/ID tracking/vision-probability gate in the urgent path: a close cut-in
  may precede selected radar/vision lead. OEM object qualification + strong raw
  braking request + rapid approach must all agree. This does not infer AEB.
  """
  def __init__(self):
    self.last_now = 0
    self.last_stamp = 0
    self.clear_since = None
    self.peak = 0
    self.sound_until = 0

  def update(self, cs, state_stamp, now, *, enabled=False, state_valid=True):
    if now < self.last_now:
      self.clear_since = None
      self.sound_until = 0
      return WarningCue()
    self.last_now = now
    data = getattr(cs, 'pv5CollisionWarning', None)
    inactive_clear = data is not None and getattr(data, 'inactiveClear', False)
    # Version zero is an old/missing producer. It must never become a warning.
    valid = (state_valid and fresh(state_stamp, now) and cs.canValid and not cs.canTimeout
             and data is not None and data.version == 1 and (data.sourceValid or inactive_clear)
             and fresh(data.sourceMonoTime, now))
    if not valid:
      # Stale data is neither a risk nor proof that a risk has cleared. In
      # particular brief dropouts cannot re-arm the chime repeatedly.
      self.clear_since = None
      self.sound_until = 0
      return WarningCue()
    stamp = data.sourceMonoTime
    if stamp < self.last_stamp:
      self.clear_since = None
      self.sound_until = 0
      return WarningCue()
    gap = stamp - self.last_stamp
    self.last_stamp = stamp
    if gap > 100_000_000:
      self.clear_since = None
    closing = -data.relativeSpeed
    measurements_valid = (data.sourceValid and data.targetValid
                          and all(math.isfinite(x) for x in (data.raw, data.distance, closing))
                          and -10.231 <= data.raw <= 10.241 and 0.0 < data.distance < 150.0
                          and -34.701 <= closing <= 16.401)
    risk = (measurements_valid and data.raw <= -3.0 and data.distance <= 40.0 and closing >= 1.0
            and data.distance / closing <= 1.5 and closing * closing / (2.0 * data.distance) >= 1.25)
    if not risk:
      self.sound_until = 0
      if not measurements_valid and not inactive_clear:
        self.clear_since = None
        return WarningCue()
      # Count time from distinct source samples, not repeated loop ticks.
      if self.clear_since is None:
        self.clear_since = stamp
      if stamp - self.clear_since >= 1_000_000_000:
        self.peak = 0
      return WarningCue()
    self.clear_since = None
    # A healthy safe interval can finish an old episode while cruising or
    # stopped. Display admission must not turn that evidence into a dropout.
    display_allowed = (not enabled and not cs.cruiseState.enabled and cs.gearShifter == 'drive'
                       and not cs.standstill and not cs.parkingBrake and not cs.brakeHoldActive
                       and math.isfinite(cs.vEgo) and math.isfinite(cs.vEgoRaw)
                       and cs.vEgo >= 2.0 and cs.vEgoRaw >= 2.0)
    if not display_allowed:
      self.sound_until = 0
      return WarningCue()
    level = 3 if data.distance / closing <= 0.75 or data.raw <= -3.5 else 2
    if level > self.peak:
      self.peak = level
      self.sound_until = now + 150_000_000
    return WarningCue(level, now < self.sound_until)
