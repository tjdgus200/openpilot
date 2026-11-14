"""Display-only SCC warning classification; PC trial thresholds, no control output."""

from dataclasses import dataclass
import math

from opendbc.car.hyundai.scc_decel import warning_active, fresh

PULSE_SECONDS = 0.15
REARM_SECONDS = 1.0


@dataclass(frozen=True)
class WarningInputs:
  stamp: int = 0
  raw: float = 0.0
  baseline: float = 0.0
  distance: float = 0.0
  relative_speed: float = 0.0
  target_valid: bool = False


def capture_inputs(parser, observation, baseline):
  """Copy only the already qualified original SCC observation, never TX fields."""
  if parser is None or not observation.valid or not math.isfinite(baseline):
    return WarningInputs()
  if parser.ts_nanos.get('SCC_CONTROL', {}).get('COUNTER', 0) != observation.stamp:
    return WarningInputs()
  values = parser.vl['SCC_CONTROL']
  try:
    distance = float(values.get('ACC_ObjDist', 0.0))
    relative_speed = float(values.get('ACC_ObjRelSpd', 0.0))
  except (TypeError, ValueError, OverflowError):
    return WarningInputs(observation.stamp, observation.raw, baseline)
  target_valid = (
    values.get('HUD_LEAD_INFO') in (1, 2)
    and math.isfinite(distance)
    and math.isfinite(relative_speed)
    and 0.5 < distance < 150.0
    and -16.4 <= relative_speed <= 34.7
  )
  return WarningInputs(observation.stamp, observation.raw, baseline, distance if target_valid else 0.0, relative_speed if target_valid else 0.0, target_valid)


def write_inputs(output, inputs):
  output.pv5SccWarningVersion = 1
  output.pv5SccWarningMonoTime = inputs.stamp
  output.pv5SccWarningRaw = inputs.raw
  output.pv5SccWarningBaseAccel = inputs.baseline
  output.pv5SccWarningDistance = inputs.distance
  output.pv5SccWarningRelSpeed = inputs.relative_speed
  output.pv5SccWarningTargetValid = inputs.target_valid


@dataclass(frozen=True)
class WarningCue:
  level: int = 0  # 0 off, 1 small/silent, 2 urgent, 3 increased urgency
  audible: bool = False
  reason: str = ''


class SccWarning:
  """One bounded risk episode; no identity claims about the SCC target vehicle."""

  def __init__(self):
    self.reset()

  def reset(self):
    self.last_now = None
    self.peak = 0
    self.clear_since = None
    self.sound_until = 0.0
    self.visual_until = 0.0
    self.visual_level = 0
    self.stationary_since = None
    self.last_target = None

  def _stationary(self, stamp, distance, relative_speed, ego_speed, now):
    stationary = abs(ego_speed + relative_speed) <= 1.0 and ego_speed >= 1.0
    previous = self.last_target
    if previous is not None and stamp != previous[0]:
      dt = (stamp - previous[0]) * 1e-9
      coherent = 0 < dt <= 0.1 and abs(distance - (previous[1] + previous[2] * dt)) <= 3.0 and abs(relative_speed - previous[2]) <= 3.0
      if not coherent:
        self.stationary_since = None
    if stationary:
      if self.stationary_since is None:
        self.stationary_since = now
    else:
      self.stationary_since = None
    self.last_target = (stamp, distance, relative_speed)
    return self.stationary_since is not None and now - self.stationary_since >= 0.15

  def _level(self, cs, output, now_ns):
    # Missing/mixed-version metadata must not quietly suppress a previously
    # qualified urgent SCC warning. It still receives episode-based sound.
    stamp = int(getattr(output, 'pv5SccWarningMonoTime', 0))
    if getattr(output, 'pv5SccWarningVersion', 0) != 1 or not fresh(stamp, now_ns) or stamp != output.pv5SccDecelSourceMonoTime:
      self.stationary_since = self.last_target = None
      return 2, 'unknown-source'
    raw, baseline = output.pv5SccWarningRaw, output.pv5SccWarningBaseAccel
    if not all(math.isfinite(v) and -4.0 <= v <= 2.5 for v in (raw, baseline)):
      self.stationary_since = self.last_target = None
      return 2, 'unknown-source'
    extra = max(0.0, baseline - output.accel)
    level, reason = (3, 'severe-request') if raw <= -3.5 else (2, 'strong-request') if raw <= -3.0 or extra >= 2.0 else (1, 'assistance')
    distance, relative_speed = output.pv5SccWarningDistance, output.pv5SccWarningRelSpeed
    ego_speed = getattr(cs, 'vEgo', float('nan'))
    if not (
      output.pv5SccWarningTargetValid
      and math.isfinite(distance)
      and 0.5 < distance < 150.0
      and math.isfinite(relative_speed)
      and -16.4 <= relative_speed <= 34.7
      and math.isfinite(ego_speed)
      and 0 <= ego_speed <= 60
    ):
      self.stationary_since = self.last_target = None
      return level, reason
    closing = -relative_speed
    ttc = distance / closing if closing >= 0.5 else float('inf')
    stationary = self._stationary(stamp, distance, relative_speed, ego_speed, now_ns * 1e-9)
    required = closing * closing / (2.0 * max(0.5, distance - 3.0)) if closing > 0 else 0.0
    if ttc <= 1.0:
      return 3, 'short-ttc'
    if (distance <= 10.0 and closing >= 0.5) or ttc <= 2.0:
      return max(level, 2), reason if level >= 2 else 'approaching'
    if stationary and closing >= 0.5 and required >= 1.5:
      return max(level, 2), reason if level >= 2 else 'stationary-estimate'
    return level, reason

  def update(self, cp, cs, output, output_stamp, now_ns, enabled=True, output_valid=True):
    now = now_ns * 1e-9
    if not enabled:
      self.reset()
      return WarningCue()
    if self.last_now is not None and (now < self.last_now or now - self.last_now > 0.5):
      self.reset()
    self.last_now = now
    active = output_valid and warning_active(cp, cs, output, output_stamp, now_ns)
    if active:
      level, reason = self._level(cs, output, now_ns)
    else:
      level, reason = 0, ''
      self.stationary_since = self.last_target = None
      self.visual_until = 0.0
    if level >= 2:
      if level > self.peak:
        self.sound_until = now + PULSE_SECONDS
        self.peak = level
      self.clear_since = None
      self.visual_until = now + 0.25
      self.visual_level = level
      return WarningCue(level, now < self.sound_until, reason)
    if self.clear_since is None:
      self.clear_since = now
    elif now - self.clear_since >= REARM_SECONDS:
      self.peak = 0
    # Brief risk-threshold jitter must not flash the whole screen. This hold
    # requires a currently qualified, fresh/admitted SCC braking output; missing
    # data, pedals and disable still clear it immediately above.
    if level == 1 and now < self.visual_until:
      return WarningCue(self.visual_level, False, 'held-risk')
    return WarningCue(level, False, reason)
