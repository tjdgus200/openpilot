import math
import time
from dataclasses import dataclass

import pyray as rl
from openpilot.common.hud_driving_feedback import LANE_CENTER_STATUS, angle_steering_demand, model_confidence_score
from openpilot.system.ui.lib.text_draw import draw_text_ui_style


MUTED = rl.Color(186, 193, 200, 255)
BLUE = rl.Color(103, 204, 255, 255)
TRACK = rl.Color(66, 76, 87, 255)
GRADE_COLORS = {
  'green': rl.Color(100, 225, 170, 255),
  'yellow': rl.Color(255, 205, 70, 255),
  'red': rl.Color(255, 90, 90, 255),
}


@dataclass(frozen=True)
class DrivingFeedback:
  confidence: float | None = None
  grade: str = ''
  steer: float | None = None
  center_label: str = '중앙 보정 --'
  center: float | None = None


def sample_driving_feedback(sm, started_frame: int, now: float, max_lateral_accel: float) -> DrivingFeedback:
  def fresh(name, limit):
    return (sm.seen[name] and sm.alive[name] and sm.valid[name] and sm.recv_frame[name] >= started_frame
            and 0.0 <= now - sm.recv_time[name] <= limit)

  model = sm['modelV2'] if fresh('modelV2', 0.5) else None
  confidence = model_confidence_score(model)
  grade = str(model.confidence) if model is not None else ''
  if not fresh('controlsState', 0.3):
    return DrivingFeedback(confidence, grade)

  controls = sm['controlsState']
  status = str(controls.pv5LaneCenterStatus)
  if status == 'waiting':
    status = str(controls.pv5LaneCenterWaitReason)
    if status == 'none':
      status = 'waiting'
  center = controls.pv5LaneCenterFraction
  if status == 'correcting' and 0 < abs(center) <= 1.0:
    center_label = '중앙 보정 ' + ('<' if center < 0 else '>')
  else:
    center = None
    center_label = LANE_CENTER_STATUS.get(status, ('중앙 보정 꺼짐' if status == 'off' else '중앙 보정 --', ''))[0]

  steer = None
  kind = controls.lateralControlState.which()
  if kind == 'angleState' and fresh('carControl', 0.3):
    if not sm['carControl'].latActive:
      steer = 0.0
    elif fresh('carState', 0.3) and fresh('liveParameters', 0.5):
      steer = angle_steering_demand(sm['carState'].vEgo, controls.desiredCurvature or controls.curvature,
                                   sm['liveParameters'].roll, max_lateral_accel)
  elif kind in ('torqueState', 'pidState') and fresh('carOutput', 0.3):
    torque = sm['carOutput'].actuatorsOutput.torque
    if math.isfinite(torque):
      steer = min(1.0, max(-1.0, torque))

  return DrivingFeedback(confidence, grade, steer, center_label, center)


def _draw_feedback(rect, font, data: DrivingFeedback, compact: bool) -> None:
  width, row, size = (204, 25, 17) if compact else (286, 37, 24)
  pad = 10 if compact else 14
  x = rect.x + rect.width - width - (20 if compact else 36)
  y = rect.y + max(104 if compact else 250, rect.height * 0.26)
  rl.draw_rectangle_rounded(rl.Rectangle(x, y, width, row * 3 + pad * 2), 0.16, 8, rl.Color(6, 11, 17, 220))
  x += pad
  y += pad
  bar_width = width - 2 * pad
  bar_height = 3 if compact else 4

  def label(text, label_x, align, color):
    draw_text_ui_style(text, label_x, y, size, color, font=font, border_width=0, shadow_offset=0,
                       align=align, y_offset=0)

  def bar(value, color, signed=False):
    bar_y = y + row - bar_height - 3
    rl.draw_rectangle_rec(rl.Rectangle(x, bar_y, bar_width, bar_height), TRACK)
    if value is not None:
      origin = x + bar_width / 2 if signed else x
      length = value * bar_width / (2 if signed else 1)
      rl.draw_rectangle_rec(rl.Rectangle(min(origin, origin + length), bar_y, abs(length), bar_height), color)

  confidence_color = GRADE_COLORS.get(data.grade, MUTED)
  label('모델 신뢰도', x, 'left_top', rl.WHITE)
  label(f'{data.confidence:.1%}' if data.confidence is not None else '--',
        x + bar_width, 'right_top', confidence_color)
  bar(data.confidence, confidence_color)
  y += row
  label('스티어', x, 'left_top', rl.WHITE)
  label(f'{data.steer:+.0%}' if data.steer is not None else '--', x + bar_width, 'right_top', BLUE)
  bar(data.steer, BLUE, signed=True)
  y += row
  label(data.center_label, x, 'left_top', BLUE if data.center is not None else MUTED)
  bar(data.center, BLUE, signed=True)


class Pv5DrivingFeedbackOverlay:
  def __init__(self, compact: bool = False):
    self.compact = compact
    self._sampled = float('-inf')
    self._started_frame = -1
    self._data = DrivingFeedback()

  def draw(self, rect, font, ui_state) -> None:
    cp = ui_state.CP
    if not ui_state.started or cp is None or cp.notCar or cp.carFingerprint != 'KIA_PV5':
      return

    now = time.monotonic()
    if self._started_frame != ui_state.started_frame or now - self._sampled >= 0.1:
      self._started_frame = ui_state.started_frame
      self._sampled = now
      self._data = sample_driving_feedback(ui_state.sm, self._started_frame, now, cp.maxLateralAccel)
    _draw_feedback(rect, font, self._data, self.compact)
