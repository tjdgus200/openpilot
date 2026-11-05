import math

from openpilot.common.realtime import DT_CTRL


PARAM = "Pv5LaneCenterAssist"
MAX_CORRECTION_ACCEL = 0.08  # m/s²
PREVIEW_SECONDS = 1.5
# Preserve the previous low-speed steering envelope as speed approaches zero.
CURVATURE_SCALE_SPEED = 5.0  # m/s, normalization only


def _fresh(sm, service, now, limit=0.2):
  return (sm.seen[service] and sm.valid[service] and sm.alive[service] and
          0.0 <= now - sm.recv_time[service] <= limit)


def _y_at(line, distance):
  xs, ys = line.x, line.y
  if len(xs) != len(ys) or not 2 <= len(xs) <= 33:
    return math.nan
  for i in range(1, len(xs)):
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    if not (math.isfinite(x0) and math.isfinite(x1) and x1 > x0 and
            math.isfinite(y0) and math.isfinite(y1)):
      break
    if x0 <= distance <= x1:
      return y0 + (y1 - y0) * (distance - x0) / (x1 - x0)
  return math.nan


def _target_accel(model, speed, curvature):
  if not (math.isfinite(curvature) and abs(curvature) * speed**2 <= 0.6):
    return 0.0, 'geometry'
  if (len(model.laneLines) != 4 or len(model.laneLineProbs) != 4 or len(model.laneLineStds) != 4 or
      not all(0.85 <= model.laneLineProbs[i] <= 1 and 0 <= model.laneLineStds[i] <= 0.2 for i in (1, 2))):
    return 0.0, 'laneQuality'
  if len(model.leadsV3) == 0 or any(not 0 <= lead.prob < 0.25 for lead in model.leadsV3):
    return 0.0, 'lead'

  left, right = model.laneLines[1], model.laneLines[2]
  preview = speed * PREVIEW_SECONDS
  offsets = []
  widths = []
  for x in (preview / 2, preview):
    l, r, path = _y_at(left, x), _y_at(right, x), _y_at(model.position, x)
    width, offset = r - l, (l + r) / 2 - path
    if not (2.8 <= width <= 4.0 and abs(offset) <= 0.30 and abs((l + r) / 2) <= 0.75):
      return 0.0, 'geometry'
    offsets.append(offset)
    widths.append(width)
  if abs(widths[1] - widths[0]) > 0.20 or abs(offsets[1] - offsets[0]) > 0.12:
    return 0.0, 'geometry'
  # Device-frame +y and model action curvature both point to the right.
  target = max(-MAX_CORRECTION_ACCEL, min(MAX_CORRECTION_ACCEL, 0.25 * 2 * offsets[1] / PREVIEW_SECONDS**2))
  return target, 'checking' if target else 'centered'


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
      target, self.status = _target_accel(model, speed, model.action.desiredCurvature)
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
