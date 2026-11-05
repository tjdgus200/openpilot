import math
import time
from dataclasses import dataclass

import pyray as rl
from openpilot.common.hud_driving_feedback import LANE_CENTER_STATUS, angle_steering_demand, model_confidence_score
from openpilot.system.ui.lib.text_draw import draw_text_ui_style


MUTED = rl.Color(186, 193, 200, 255)
BLUE = rl.Color(103, 204, 255, 255)
AMBER = rl.Color(255, 205, 70, 255)
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


def _draw_centered_feedback(rect, font, data: DrivingFeedback, compact: bool) -> None:
  """Match the Jetson layout with two vertical gauges and the correction bar below."""
  scale = min(rect.height / 490, rect.width / 900, 1.0)
  cx = rect.x + rect.width / 2
  vertical = scale
  top = rect.y + rect.height / 2 - 173 * scale
  if compact:
    # The speed and gear row starts near the middle of the small display.
    vertical = min(scale, rect.height / 850)
    top = rect.y + rect.height * .095
  gauge_width = 116 * scale
  gauge_height = 206 * vertical
  gap = 7 * vertical
  inset = 10 * scale

  def label(text, x, y, size, color):
    draw_text_ui_style(text, x, y, size * scale, color, font=font,
                       border_width=0, shadow_offset=0, align='center_top', y_offset=0)

  def fill(x, y, width, height, color):
    if width > 0 and height > 0:
      rl.draw_rectangle_rounded(rl.Rectangle(x, y, width, height), 0.25, 6, color)

  confidence_x, steer_x = cx - 118 * scale, cx + 118 * scale
  confidence_color = GRADE_COLORS.get(data.grade, MUTED)
  steer_color = BLUE if data.steer is not None and data.steer > 0 else MUTED
  if data.steer is not None and data.steer < 0:
    steer_color = AMBER
  for x, value, name, color in (
    (confidence_x, f'{data.confidence:.1%}' if data.confidence is not None else '--', '모델 신뢰도', confidence_color),
    (steer_x, f'{data.steer:+.0%}' if data.steer is not None else '--', '스티어', steer_color),
  ):
    value_y = top - (59 if compact else 48) * vertical
    label(value, x, value_y, 34, color)
    label(name, x, top + gauge_height + 11 * vertical, 27, rl.WHITE)
    gauge = rl.Rectangle(x - gauge_width / 2, top, gauge_width, gauge_height)
    rl.draw_rectangle_rounded_lines_ex(gauge, 0.22, 8, 2.5 * scale, MUTED)

  segment_height = (gauge_height - 2 * inset - 2 * gap) / 3
  inner_width = gauge_width - 2 * inset
  for i in range(3):
    x = confidence_x - inner_width / 2
    bottom = top + gauge_height - inset - i * (segment_height + gap)
    level = min(1.0, max(0.0, (data.confidence or 0.0) * 3 - i))
    fill(x, bottom - segment_height, inner_width, segment_height, TRACK)
    fill(x, bottom - segment_height * level, inner_width, segment_height * level, confidence_color)

  mid = top + gauge_height / 2
  rl.draw_rectangle_rec(rl.Rectangle(steer_x - gauge_width / 2, mid, gauge_width, 2 * scale), TRACK)
  if data.steer is not None:
    height = abs(data.steer) * (gauge_height / 2 - inset)
    fill(steer_x - inner_width / 2, mid - height if data.steer > 0 else mid, inner_width, height, steer_color)

  center_y = top + gauge_height + 69 * vertical
  label(data.center_label, cx, center_y, 25 if compact else 29, BLUE if data.center is not None else MUTED)
  bar_y = center_y + 43 * vertical
  half = 153 * scale
  fill(cx - half, bar_y, half * 2, 10 * scale, TRACK)
  if data.center is not None:
    width = data.center * half
    fill(cx + min(0, width), bar_y, abs(width), 10 * scale, BLUE)


class Pv5DrivingFeedbackOverlay:
  def __init__(self, compact: bool = False):
    self.compact = compact
    self.centered = False
    self.warning = False
    self._sampled = float('-inf')
    self._started_frame = -1
    self._data = DrivingFeedback()

  def visible(self, ui_state) -> bool:
    cp = ui_state.CP
    return not self.warning and ui_state.started and cp is not None and not cp.notCar and cp.carFingerprint == 'KIA_PV5'

  def draw(self, rect, font, ui_state) -> None:
    if not self.visible(ui_state):
      return

    now = time.monotonic()
    if self._started_frame != ui_state.started_frame or now - self._sampled >= 0.1:
      self._started_frame = ui_state.started_frame
      self._sampled = now
      self._data = sample_driving_feedback(ui_state.sm, self._started_frame, now, ui_state.CP.maxLateralAccel)
    if self.centered:
      _draw_centered_feedback(rect, font, self._data, self.compact)
    else:
      _draw_feedback(rect, font, self._data, self.compact)
