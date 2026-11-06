import math
import time
from dataclasses import dataclass

import pyray as rl
from openpilot.common.constants import CV
from openpilot.common.hud_driving_feedback import (
  DRIVING_NOTICE_TEXT, DRIVING_POPUP_COLORS, DrivingNoticeTracker, DrivingPopupSlide, driving_notice_events,
  LANE_CENTER_STATUS, STEERING_WARNING_TEXT, angle_steering_demand, lane_line_probabilities,
  lane_confidence_score, lane_confidence_grade, lane_probability_text, pv5_steering_warning,
)
from openpilot.selfdrive.carrot.deceleration_source import deceleration_banner_text, deceleration_source_presentation
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
  left_lane_probability: float | None = None
  right_lane_probability: float | None = None
  steer: float | None = None
  center_label: str = '중앙 보정 --'
  center: float | None = None

  @property
  def lane_confidence(self) -> float | None:
    return lane_confidence_score(self.left_lane_probability, self.right_lane_probability)


def _fresh(sm, name, started_frame: int, now: float, limit: float) -> bool:
  return (sm.seen[name] and sm.alive[name] and sm.valid[name] and sm.recv_frame[name] >= started_frame
          and 0.0 <= now - sm.recv_time[name] <= limit)


def sample_driving_feedback(sm, started_frame: int, now: float, max_lateral_accel: float) -> DrivingFeedback:
  def fresh(name, limit):
    return _fresh(sm, name, started_frame, now, limit)

  model = sm['modelV2'] if fresh('modelV2', 0.5) else None
  left, right = lane_line_probabilities(model)
  if not fresh('controlsState', 0.3):
    return DrivingFeedback(left, right)

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

  return DrivingFeedback(left, right, steer, center_label, center)


def sample_deceleration_popup(sm, started_frame: int, now: float, set_speed_kph: float | None) -> str | None:
  if set_speed_kph is None or not _fresh(sm, 'carControl', started_frame, now, .3) or not sm['carControl'].longActive:
    return None
  if not all(_fresh(sm, name, started_frame, now, limit)
             for name, limit in (('carState', .3), ('longitudinalPlan', .5), ('carrotMan', 1.))):
    return None
  carrot, plan = sm['carrotMan'], sm['longitudinalPlan']
  desired_speed = carrot.desiredSpeed
  if not 0 < desired_speed < 200:
    return None
  label, _ = deceleration_source_presentation(carrot.desiredSource)
  return deceleration_banner_text(label, sm['carState'].vEgo * CV.MS_TO_KPH, set_speed_kph,
                                  desired_speed, plan.cruiseTarget, plan.aTarget)


def _draw_feedback(rect, font, data: DrivingFeedback, compact: bool) -> None:
  width, row, size = (204, 25, 17) if compact else (348, 46, 29)
  pad = 10 if compact else 18
  x = rect.x + rect.width - width - (20 if compact else 36)
  y = rect.y + max(104 if compact else 250, rect.height * 0.26)
  rl.draw_rectangle_rounded(rl.Rectangle(x, y, width, row * 4 + pad * 2), 0.16, 8, rl.Color(6, 11, 17, 220))
  x += pad
  y += pad
  bar_width = width - 2 * pad
  bar_height = 3 if compact else 5

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

  confidence_color = GRADE_COLORS.get(lane_confidence_grade(data.lane_confidence), MUTED)
  label('차선 인식', x, 'left_top', rl.WHITE)
  y += row
  label(f'좌 {lane_probability_text(data.left_lane_probability)}', x, 'left_top',
        GRADE_COLORS.get(lane_confidence_grade(data.left_lane_probability), MUTED))
  label(f'우 {lane_probability_text(data.right_lane_probability)}', x + bar_width, 'right_top',
        GRADE_COLORS.get(lane_confidence_grade(data.right_lane_probability), MUTED))
  bar(data.lane_confidence, confidence_color)
  y += row
  label('스티어', x, 'left_top', rl.WHITE)
  label(f'{data.steer:+.0%}' if data.steer is not None else '--', x + bar_width, 'right_top', BLUE)
  bar(data.steer, BLUE, signed=True)
  y += row
  label(data.center_label, x, 'left_top', BLUE if data.center is not None else MUTED)
  bar(data.center, BLUE, signed=True)


def _draw_large_feedback(rect, font, data: DrivingFeedback, compact: bool, right_reserved: int) -> None:
  """Match the Jetson layout with two vertical gauges and the correction bar below."""
  scale = min(rect.height / 490, rect.width / 900, 1.0 if compact else 1.30)
  vertical = min(scale, rect.height / 850) if compact else scale
  column_offset = 95
  cx = rect.x + rect.width - max(right_reserved, (12 if compact else 22) * scale) - max(column_offset + 58, 153) * scale
  top = rect.y + rect.height - 318 * vertical - 32 * scale
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

  confidence_x, steer_x = cx - column_offset * scale, cx + column_offset * scale
  confidence_color = GRADE_COLORS.get(lane_confidence_grade(data.lane_confidence), MUTED)
  steer_color = BLUE if data.steer is not None and data.steer > 0 else MUTED
  if data.steer is not None and data.steer < 0:
    steer_color = AMBER
  for x, value, name, color in (
    (confidence_x, '', '차선 인식', confidence_color),
    (steer_x, f'{data.steer:+.0%}' if data.steer is not None else '--', '스티어', steer_color),
  ):
    value_y = top - (59 if compact else 48) * vertical
    if value:
      label(value, x, value_y, 34, color)
    label(name, x, top + gauge_height + 11 * vertical, 27, rl.WHITE)
    gauge = rl.Rectangle(x - gauge_width / 2, top, gauge_width, gauge_height)
    rl.draw_rectangle_rounded_lines_ex(gauge, 0.22, 8, 2.5 * scale, MUTED)

  for probability, offset, name in ((data.left_lane_probability, 85, '좌'), (data.right_lane_probability, 47, '우')):
    color = GRADE_COLORS.get(lane_confidence_grade(probability), MUTED)
    label(f'{name} {lane_probability_text(probability)}', confidence_x, top - offset * vertical, 30, color)

  segment_height = (gauge_height - 2 * inset - 2 * gap) / 3
  inner_width = gauge_width - 2 * inset
  for i in range(3):
    x = confidence_x - inner_width / 2
    bottom = top + gauge_height - inset - i * (segment_height + gap)
    level = min(1.0, max(0.0, (data.lane_confidence or 0.0) * 3 - i))
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
    self.large = False
    self.warning = False
    self._sampled = float('-inf')
    self._started_frame = -1
    self._data = DrivingFeedback()
    self.popup_slide = DrivingPopupSlide()
    self._driving_notices = DrivingNoticeTracker()
    self._popup_started_frame = -1

  def _pv5_started(self, ui_state) -> bool:
    cp = ui_state.CP
    return ui_state.started and cp is not None and not cp.notCar and cp.carFingerprint == 'KIA_PV5'

  def visible(self, ui_state) -> bool:
    return not self.warning and self._pv5_started(ui_state)

  def draw(self, rect, font, ui_state, *, right_reserved: int = 0) -> None:
    if not self.visible(ui_state):
      return

    now = time.monotonic()
    if self._started_frame != ui_state.started_frame or now - self._sampled >= 0.1:
      self._started_frame = ui_state.started_frame
      self._sampled = now
      self._data = sample_driving_feedback(ui_state.sm, self._started_frame, now, ui_state.CP.maxLateralAccel)
    if self.large:
      _draw_large_feedback(rect, font, self._data, self.compact, right_reserved)
    else:
      _draw_feedback(rect, font, self._data, self.compact)

  def draw_popup(self, rect, font, ui_state, set_speed_kph: float | None) -> None:
    if not self._pv5_started(ui_state):
      self.popup_slide.reset()
      self._driving_notices.reset()
      return
    now = time.monotonic()
    sm, started_frame = ui_state.sm, ui_state.started_frame
    if started_frame != self._popup_started_frame:
      self._popup_started_frame = started_frame
      self._driving_notices.reset()
      self.popup_slide.reset()
    events = driving_notice_events(sm, now, started_frame=started_frame)
    if self.warning:
      self._driving_notices.update(events, now, blocked=True)
      self.popup_slide.reset()
      return
    warning = pv5_steering_warning(sm, now, cp=ui_state.CP, started_frame=started_frame)
    deceleration = None if warning else sample_deceleration_popup(sm, started_frame, now, set_speed_kph)
    notice = self._driving_notices.update(events, now, blocked=bool(warning), decelerating=bool(deceleration))
    text = STEERING_WARNING_TEXT.get(warning) or DRIVING_NOTICE_TEXT.get(notice) or deceleration
    if not text:
      self.popup_slide.reset()
      return
    width, height, size = (330, 42, 24) if self.compact else (1080, 136, 70)
    scale = min(1., rect.width / (width + 40))
    width, height, size = width * scale, height * scale, size * scale
    x = rect.x + (rect.width - width) / 2 - (15 if self.compact else 0)
    y = self.popup_slide.top(now, rect.y + (6 if self.compact else 24), height, urgent=warning == 2)
    background, accent = (rl.Color(*color) for color in DRIVING_POPUP_COLORS[warning])
    box = rl.Rectangle(x, y, width, height)
    rl.draw_rectangle_rounded(box, 0.26, 8, background)
    rl.draw_rectangle_rounded_lines_ex(box, 0.26, 8, 2 * scale, accent)
    draw_text_ui_style(text, x + width / 2, y + height / 2, size, rl.WHITE,
                       font=font, border_width=0, shadow_offset=0, align='center', y_offset=0)
