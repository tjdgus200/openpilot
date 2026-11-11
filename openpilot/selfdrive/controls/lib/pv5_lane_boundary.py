"""Opt-in PV5 lane-edge correction; never a lane-center controller.

Geometry uses estimated body clearance, not a measured tire position. Trial
thresholds/plant results are not physical validation. Every existing downstream
driver-override, curvature, angle, rate and CAN safety limit remains authoritative.
"""
import math

import numpy as np

PARAM = 'Pv5LaneBoundaryAssist'
HALF_BODY = 1.895 / 2  # Kia KR PV5 Cargo specification, excludes mirrors
MARGIN = .20  # m, experimental body-to-line clearance
MAX_ACCEL = .30  # m/s^2, additional correction only; not a vehicle safety limit
BUILD_JERK = .50  # m/s^3; existing final curvature slew is also retained


class Pv5LaneBoundary:
  def __init__(self, params):
    self.params = params
    self.enabled = False
    self.next_poll = 0.
    self.last_time = None
    self.last_stamp = None
    self.last_frame = None
    self.last_signal = -math.inf
    self.bounds = None
    self.target = self.normalized_accel = self.accel = self.fraction = 0.
    self.status = 'off'

  def clear(self, status):
    self.target = self.normalized_accel = self.accel = self.fraction = 0.
    self.bounds = None
    self.status = status
    return 0.

  @staticmethod
  def _geometry(model, speed):
    # Look only a short distance ahead. No invented opposite line, learned width,
    # model-path centering, lead selection or extra neural inference is used.
    end = float(np.clip(speed * .8, 8., 20.))
    xs = np.linspace(end * .5, end, 5)
    constraints = []
    observed = []
    for index, direction in ((1, 1.), (2, -1.)):
      probability, std = model.laneLineProbs[index], model.laneLineStds[index]
      if not (math.isfinite(probability) and .75 <= probability <= 1. and math.isfinite(std) and 0 <= std <= .35):
        continue
      line = model.laneLines[index]
      x, y = np.asarray(line.x), np.asarray(line.y)
      if (len(x) < 5 or x.shape != y.shape or not np.isfinite(x).all() or not np.isfinite(y).all() or
          not (np.diff(x) > 0).all() or x[0] > 0. or x[-1] < end):
        continue
      edge = np.interp(xs, x, y)
      at_car = float(np.interp(0., x, y))
      observed.append((index, at_car, edge))
      clearance_now = -direction * at_car - HALF_BODY
      # A line already well underneath/across the body may belong to a completed
      # lane change. Do not pull back into a lane identified only by this line.
      if clearance_now < -.35:
        return None, 'geometry'
      safe_edge = edge + direction * (HALF_BODY + MARGIN)
      values = 2. * safe_edge / xs**2
      constraints.append((direction, float(np.max(values) if direction > 0 else np.min(values))))
    if not constraints:
      return None, 'laneQuality'
    if len(observed) == 2:
      width = observed[1][2] - observed[0][2]
      width_now = observed[1][1] - observed[0][1]
      if not (2.6 <= width_now <= 4.8 and (width >= 2.6).all() and (width <= 4.8).all()):
        return None, 'geometry'
    lower = max((value for direction, value in constraints if direction > 0), default=-math.inf)
    upper = min((value for direction, value in constraints if direction < 0), default=math.inf)
    if lower > upper:
      return None, 'geometry'  # incompatible edges, never pick arbitrarily
    return (lower, upper), 'centered'

  def update(self, sm, cs, model, now, *, active, curvature):
    previous = self.last_time
    self.last_time = now
    if not math.isfinite(now):
      return self.clear('inputs')
    if now >= self.next_poll or previous is None or now < previous:
      self.next_poll = now + .25
      self.enabled = self.params.get_bool(PARAM)
    if not self.enabled:
      return self.clear('off')
    if previous is None or not 0 < now - previous <= .1:
      return self.clear('inputs')
    if not active:
      return self.clear('inactive')
    if not all(sm.seen[k] and sm.valid[k] and sm.alive[k] and 0 <= now-sm.recv_time[k] <= .2
               for k in ('carState', 'modelV2')):
      return self.clear('inputs')
    if not (cs.canValid and not cs.canTimeout and math.isfinite(cs.vEgo) and 0 <= cs.vEgo <= 80. and
            math.isfinite(curvature) and abs(curvature) <= .2):
      return self.clear('inputs')
    if cs.leftBlinker or cs.rightBlinker:
      self.last_signal = now
      return self.clear('maneuver')
    if (now-self.last_signal < 1. or str(model.meta.desire) != 'none' or
        str(model.meta.laneChangeState) != 'off' or str(model.meta.autoTurnSignal) != 'none'):
      return self.clear('maneuver')
    if cs.steeringPressed:
      return self.clear('driver')
    if cs.standstill or cs.vEgo == 0. or str(cs.gearShifter) != 'drive':
      return self.clear('stopped')
    stamp, frame = sm.recv_time['modelV2'], model.frameId
    if self.last_stamp is not None and (stamp < self.last_stamp or frame < self.last_frame):
      self.last_stamp, self.last_frame = stamp, frame
      return self.clear('inputs')
    if self.last_stamp != stamp:
      if self.last_frame == frame:
        self.last_stamp = stamp
        return self.clear('inputs')
      try:
        self.bounds, self.status = self._geometry(model, cs.vEgo)
      except (IndexError, TypeError, ValueError, AttributeError, FloatingPointError):
        return self.clear('inputs')
      self.last_stamp, self.last_frame = stamp, frame
    if self.bounds is None:
      return self.clear(self.status)
    # Only scalar corridor projection at 100Hz; lane arrays are processed once
    # per fresh model frame. Planner switches immediately use the new base path.
    lower, upper = self.bounds
    desired = min(max(curvature, lower), upper)
    # Numeric normalization, NOT an engagement threshold. Below1m/s this keeps
    # curvature slew finite without dividing by a tiny/underflowed speed squared.
    # Above1m/s command/ramp math is identical to the preceding release.
    scale = max(cs.vEgo, 1.)**2
    self.target = min(MAX_ACCEL, max(-MAX_ACCEL, (desired-curvature)*scale))
    if not math.isfinite(self.target):
      return self.clear('inputs')
    if self.target == 0.:
      self.normalized_accel = 0.
      self.status = 'centered'
    else:
      if self.normalized_accel * self.target < 0:
        self.normalized_accel = 0.
      step = BUILD_JERK * (now-previous)
      self.normalized_accel = math.copysign(min(abs(self.target), abs(self.normalized_accel)+step), self.target)
      self.status = 'correcting'
    correction = self.normalized_accel / scale
    self.accel = correction * cs.vEgo**2
    self.fraction = self.accel / MAX_ACCEL
    return correction
