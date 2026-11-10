"""Read modeld's Blendful telemetry for the existing comma/Jetson gauges.

This class does not calculate, clamp or add any steering correction.
"""
import math

from openpilot.selfdrive.modeld.blendful import LOOKAHEAD_TIME, MIN_LOOKAHEAD

PARAM = 'Pv5LaneCenterAssist'
DISPLAY_FULL_PULL = .2  # ZoomPilot's COLOR_FULL; display only, never a control cap


class Pv5LaneCenter:
  def __init__(self, params=None):
    self.enabled = False
    self.status = 'off'
    self.fraction = 0.
    self.accel = 0.

  def observe(self, sm, cs, model, now, *, active, lane_mode):
    self.fraction = self.accel = 0.
    data = model.pv5LaneCenter
    self.enabled = data.enabled
    if not self.enabled:
      self.status = 'off'
      return
    if not (data.valid and sm.seen['modelV2'] and sm.valid['modelV2'] and sm.alive['modelV2'] and
            0 <= now - sm.recv_time['modelV2'] <= .2 and
            math.isfinite(data.curvature) and math.isfinite(data.trust) and math.isfinite(cs.vEgo)):
      self.status = 'inputs'
      return
    if not active:
      self.status = 'inactive'
      return
    if lane_mode:
      self.status = 'laneMode'  # Carrot selected lateralPlan instead of model action
      return
    # Same geometric push and sqrt strength used by ZoomPilot's onroad gauge.
    pull = data.curvature * max(cs.vEgo * LOOKAHEAD_TIME, MIN_LOOKAHEAD)**2 / 2
    accel = data.curvature * cs.vEgo**2
    if not math.isfinite(pull) or not math.isfinite(accel):
      self.status = 'inputs'
      return
    self.fraction = math.copysign(math.sqrt(min(abs(pull) / DISPLAY_FULL_PULL, 1.)), pull)
    self.accel = accel
    self.status = ('correcting' if data.curvature else
                   'maneuver' if not data.active else
                   'checking' if data.trust == 0 else 'centered')
