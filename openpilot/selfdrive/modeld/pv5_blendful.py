"""PV5 settings/publication boundary for the unmodified upstream Blendful policy.

No lead, BSD, DM, pedal, gear, lane-mode or extra acceleration policy lives here.
Vehicle steering admission and driver override remain in the existing controllers.
"""
from functools import partial
import math

import numpy as np

from openpilot.cereal import log
from openpilot.selfdrive.modeld.blendful import Blendful, MIN_SPEED

PARAM = 'Pv5LaneCenterAssist'


def for_vehicle(cp, params):
  return Pv5Blendful(params) if cp.carFingerprint == 'KIA_PV5' and not cp.notCar else None


class Pv5Blendful:
  def __init__(self, params):
    self.params = params
    self.core = Blendful()
    self.enabled = False
    self.next_settings_check = 0.
    self.valid = False
    self.active = False

  def callback(self, output, speed, desire, lane_change_state, now):
    # Keep the owner's existing web toggle/name/live polling. The activation
    # expression and update arguments below match ZoomPilot ModelSettings.
    if now >= self.next_settings_check:
      self.next_settings_check = now + .25
      self.enabled = self.params.get_bool(PARAM)
    self.active = desire == log.Desire.none and lane_change_state == log.LaneChangeState.off
    return partial(self._update, output, speed, self.active)

  @staticmethod
  def _layout(output):
    lines, stds = output['lane_lines'], output['lane_lines_stds']
    probs, plan = output['lane_lines_prob'], output['plan']
    return (lines.shape == (1, 4, 33, 2) and stds.shape == lines.shape and
            probs.shape == (1, 8) and plan.ndim == 3 and plan.shape[0:2] == (1, 33) and plan.shape[2] >= 2 and
            all(np.isfinite(x).all() for x in (lines[0, 1:3, :, 0], stds[0, 1:3, :, 0],
                                              probs[0, 3:6:2], plan[0, :, :2])) and
            (stds[0, 1:3, :, 0] > 0).all())

  def _update(self, output, speed, active, curvature):
    # This is a malformed-tensor/numerical-fault boundary, not another driving
    # policy. Reset on undefined arithmetic rather than publishing NaN steering.
    try:
      if not (math.isfinite(speed) and math.isfinite(curvature)):
        raise ValueError('nonfinite action or speed')
      if self.enabled and active and speed >= MIN_SPEED and not self._layout(output):
        raise ValueError('invalid Blendful tensor layout')
      with np.errstate(divide='raise', invalid='raise', over='raise'):
        correction = self.core.update(output, speed, active, curvature, enabled=self.enabled)
      if not all(math.isfinite(float(x)) for x in (correction, self.core.trust, self.core.model_curvature)):
        raise ValueError('nonfinite Blendful result')
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, FloatingPointError, np.linalg.LinAlgError):
      self.core = Blendful()
      self.valid = False
      return 0.
    self.valid = True
    return correction

  def fill(self, target):
    target.valid = self.valid
    target.enabled = self.enabled
    target.active = self.active
    target.curvature = float(self.core.curvature)
    target.modelCurvature = float(self.core.model_curvature)
    target.trust = float(self.core.trust)
    target.goingAround = bool(self.core.going_around)
