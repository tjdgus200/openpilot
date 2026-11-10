"""PV5-only bounded center assist; fit adapted from MIT-licensed Blendful.

Portions Copyright (c) 2021-, Haibin Wen, sunnypilot and contributors.
Upstream: sunnypilot/sunnypilot 97135965effb (MIT, LICENSE.md).
"""
import math

from openpilot.common.realtime import DT_CTRL, DT_MDL


PARAM = "Pv5LaneCenterAssist"
MAX_CORRECTION_ACCEL = 0.08  # m/s²
PREVIEW_SECONDS = 1.5
# Preserve the previous low-speed steering envelope as speed approaches zero.
CURVATURE_SCALE_SPEED = 5.0  # m/s, normalization only
FIT_POINTS = 9
MIN_LINE_PROB = 0.5  # both lines remain mandatory; no single-line extrapolation


# Multi-point curvature fit and probability/std weighting adapted from
# sunnypilot Blendful (MIT), 97135965effb, via zoompilot 92e1ecbd0459.
# Admission, bilateral geometry checks and the acceleration cap remain PV5's.


def _fresh(sm, service, now, limit=0.2):
  return (sm.seen[service] and sm.valid[service] and sm.alive[service] and
          0.0 <= now - sm.recv_time[service] <= limit)


def _valid_line(line):
  xs, ys = line.x, line.y
  if len(xs) != len(ys) or not 2 <= len(xs) <= 33:
    return False
  return (all(math.isfinite(x) and math.isfinite(y) for x, y in zip(xs, ys, strict=True)) and
          all(xs[i] > xs[i - 1] for i in range(1, len(xs))))


def _sample_line(line, distances):
  # Sorted, bounded inputs: one forward pass, no numpy fit or 100Hz matrix work.
  xs, ys = line.x, line.y
  result, index = [], 1
  for x in distances:
    while index < len(xs) - 1 and xs[index] < x:
      index += 1
    fraction = (x - xs[index - 1]) / (xs[index] - xs[index - 1])
    result.append(ys[index - 1] + (ys[index] - ys[index - 1]) * fraction)
  return result


def _target_accel(model, speed, curvature):
  if not (math.isfinite(curvature) and math.isfinite(speed) and 0 < speed < 1000):
    return 0.0, 'geometry', 0.0
  if (len(model.laneLines) != 4 or len(model.laneLineProbs) != 4 or len(model.laneLineStds) != 4 or
      not all(MIN_LINE_PROB <= model.laneLineProbs[i] <= 1 and 0 <= model.laneLineStds[i] < 0.6 for i in (1, 2))):
    return 0.0, 'laneQuality', 0.0
  if len(model.leadsV3) == 0 or any(not 0 <= lead.prob < 0.25 for lead in model.leadsV3):
    return 0.0, 'lead', 0.0

  left, right = model.laneLines[1], model.laneLines[2]
  lines = (left, right, model.position)
  if not all(_valid_line(line) for line in lines):
    return 0.0, 'geometry', 0.0
  reference_speed = max(speed, CURVATURE_SCALE_SPEED)
  preview = max(reference_speed * PREVIEW_SECONDS, 10.0)
  near, far = preview / 2, min(1.5 * preview, *(line.x[-1] for line in lines))
  if far < preview or any(line.x[0] > near for line in lines):
    return 0.0, 'geometry', 0.0
  distances = [near + (far - near) * i / (FIT_POINTS - 1) for i in range(FIT_POINTS)]
  left_y, right_y, path_y = (_sample_line(line, distances) for line in lines)
  probs = [model.laneLineProbs[i] * max(0.0, min(1.0, (0.6 - model.laneLineStds[i]) / 0.4)) for i in (1, 2)]
  plan_share = 0.3 + 0.2 * max(0.0, min(1.0, (abs(curvature) * preview**2 / 2 - 0.5) / 1.5))
  widths, offsets = [], []
  numerator = denominator = 0.0
  for x, l, r, path in zip(distances, left_y, right_y, path_y, strict=True):
    width, offset = r - l, (l + r) / 2 - path
    if not (2.8 <= width <= 4.0 and abs(offset) <= 0.30):
      return 0.0, 'geometry', 0.0
    offsets.append(offset)
    widths.append(width)
    center = (l + r) / 2
    # Fit residual to delta-curvature*x^2/2, using model action AND its plan.
    residual = center - ((1 - plan_share) * curvature * x**2 / 2 + plan_share * path)
    if not math.isfinite(residual):
      return 0.0, 'geometry', 0.0
    error = max(-1.0, min(1.0, residual))
    u2 = (x / preview)**2
    # Published laneLineStds are per-line scalars, not raw per-point stds.
    # Their common fit weight cancels; apply bilateral quality below instead.
    numerator += error * u2
    denominator += u2**2
  if max(widths) - min(widths) > 0.20 or max(offsets) - min(offsets) > 0.12:
    return 0.0, 'geometry', 0.0
  delta_curvature = 2 * numerator / (denominator * preview**2)
  if abs(delta_curvature) * reference_speed**2 < 1e-8:
    delta_curvature = 0.0  # avoid a nonzero gauge from interpolation roundoff
  # Device-frame +y and model action curvature both point to the right.
  target = max(-MAX_CORRECTION_ACCEL, min(MAX_CORRECTION_ACCEL, delta_curvature * reference_speed**2))
  return target, 'checking' if target else 'centered', min(probs)


class Pv5LaneCenter:
  def __init__(self, params):
    self.params = params
    self.enabled = False
    self.next_settings_check = 0.0
    self._reset()

  def _reset(self, status='off'):
    self.model_generation = None
    self.eligible_since = None
    self.target = 0.0
    self.trust = 0.0
    self.fit_time = None
    self.status = status
    return 0.0

  def update(self, sm, cs, model, now, *, active, lane_mode):
    if now >= self.next_settings_check:
      self.next_settings_check = now + 0.25
      self.enabled = self.params.get_bool(PARAM)
    if not self.enabled:
      return self._reset()
    if not cs.canValid or cs.canTimeout or not _fresh(sm, 'carState', now, 0.1):
      return self._reset('inputs')
    speed = cs.vEgo
    if cs.gearShifter != 'drive' or cs.standstill or not math.isfinite(speed) or speed <= 0.0:
      return self._reset('stopped')
    # Follow actual lateral admission, including AlwaysLateral without cruise.
    if not active:
      return self._reset('inactive')
    if lane_mode:
      return self._reset('laneMode')

    meta = model.meta
    radar = sm['radarState']
    dm = sm['driverMonitoringState']
    errors = radar.radarErrors
    if not all(_fresh(sm, s, now) for s in ('modelV2', 'radarState', 'driverMonitoringState', 'lateralPlan', 'selfdriveState')):
      return self._reset('inputs')
    if cs.leftBlindspot or cs.rightBlindspot:
      return self._reset('blindspot')
    if (cs.steeringPressed or cs.brakePressed or dm.lockout or dm.alertLevel != 'none' or
        (dm.activePolicy == 'vision' and (dm.cameraUnavailable or dm.visionPolicyState.isDistracted))):
      return self._reset('driver')
    if (cs.leftBlinker or cs.rightBlinker or meta.laneChangeState != 'off' or meta.desire != 'none' or
        meta.leverTurn or meta.autoTurnSignal != 'none' or meta.hardBrakePredicted):
      return self._reset('maneuver')
    if errors.canError or errors.radarFault or errors.wrongConfig or errors.radarUnavailableTemporary:
      return self._reset('radar')
    if any(lead.status and not (math.isfinite(lead.dRel) and lead.dRel > max(60, speed * 4))
           for lead in (radar.leadOne, radar.leadTwo)):
      return self._reset('lead')

    generation = sm.recv_frame['modelV2'], model.frameId
    if self.model_generation is None or generation[1] != self.model_generation[1]:
      self.model_generation = generation
      elapsed = DT_MDL if self.fit_time is None else now - self.fit_time
      if not 0 < elapsed <= 0.2:
        return self._reset('inputs')
      self.fit_time = now
      target, self.status, quality = _target_accel(model, speed, model.action.desiredCurvature)
      # Confidence falls immediately and recovers gradually; never retain a
      # single missing/invalid line, stale frame, driver or obstacle permission.
      self.trust = min(quality, self.trust + (quality - self.trust) * elapsed / (0.5 + elapsed))
      target *= self.trust
      if target == 0.0 or self.target * target < 0.0:
        self.eligible_since = None
      if target != 0.0 and self.eligible_since is None:
        self.eligible_since = now
      self.target = target
    elif generation != self.model_generation:
      # A republished camera frame does not renew the assist.
      self._reset('inputs')
      self.model_generation = generation
      return 0.0

    if self.eligible_since is None:
      return 0.0
    ramp = min(1.0, max(0.0, (now - self.eligible_since - 0.5 + DT_CTRL) / 0.5))
    self.status = 'correcting' if ramp else 'checking'
    return ramp * self.target / max(speed, CURVATURE_SCALE_SPEED)**2
