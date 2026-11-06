import time

import pyray as rl

from openpilot.cereal import log
from openpilot.common.hud_attention_icon import DriverAttentionIcon, draw_driver_attention_icon
from openpilot.selfdrive.ui.onroad.alert_renderer import (
  ALERT_CRITICAL_TIMEOUT, SELFDRIVE_STATE_TIMEOUT, SELFDRIVE_UNRESPONSIVE_TIMEOUT,
)
from openpilot.system.hardware import TICI
from openpilot.system.ui.lib.application import FONT_SCALE, FontWeight, gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_draw import draw_text_ui_style
from openpilot.system.ui.lib.wrap_text import wrap_text


_attention_icon = DriverAttentionIcon()


def _critical_alert(ui_state, now: float) -> tuple[str, str, str] | None:
  if not ui_state.started:
    return None
  sm = ui_state.sm
  if sm.recv_frame['selfdriveState'] < ui_state.started_frame:
    return None
  state = sm['selfdriveState']
  if TICI and not sm.updated['selfdriveState']:
    age = now - sm.recv_time['selfdriveState']
    if age > SELFDRIVE_STATE_TIMEOUT:
      if state.enabled and age < SELFDRIVE_UNRESPONSIVE_TIMEOUT:
        return ALERT_CRITICAL_TIMEOUT.text1, ALERT_CRITICAL_TIMEOUT.text2, ''
      return None
  if state.alertStatus == log.SelfdriveState.AlertStatus.critical:
    return state.alertText1 or tr('TAKE CONTROL IMMEDIATELY'), state.alertText2, state.alertType
  return None


def critical_alert_text(ui_state, now: float) -> tuple[str, str] | None:
  alert = _critical_alert(ui_state, now)
  return alert[:2] if alert is not None else None


def draw_critical_alert(ui_state) -> bool:
  now = time.monotonic()
  alert = _critical_alert(ui_state, now)
  icon_scale = _attention_icon.scale(alert[2] if alert else '', now)
  if alert is None:
    return False

  width, height = gui_app.width, gui_app.height
  rl.draw_rectangle(0, 0, width, height, rl.Color(183, 0, 24, 255))
  font = gui_app.font(FontWeight.BOLD)
  title, detail, _ = alert
  icon_height = min(height * .24, width * .15) if icon_scale else 0.
  icon_space = icon_height * 1.16
  title_size = max(12, int(min(width * 0.075, height * 0.21)))
  while True:
    detail_size = round(title_size * 0.58)
    title_lines = wrap_text(font, title, title_size, int(width * 0.88))
    detail_lines = wrap_text(font, detail, detail_size, int(width * 0.88))
    title_line_height = title_size * FONT_SCALE * 1.1
    detail_line_height = detail_size * FONT_SCALE * 1.1
    gap = detail_size * 0.6 if detail_lines else 0
    total_height = len(title_lines) * title_line_height + gap + len(detail_lines) * detail_line_height
    if total_height + icon_space <= height * 0.84 or title_size <= 12:
      break
    title_size = max(12, int(title_size * 0.88))

  y = (height - total_height - icon_space) / 2
  if icon_scale:
    draw_driver_attention_icon(width / 2, y + icon_height / 2, icon_height * icon_scale)
    y += icon_space
  for lines, size, line_height in ((title_lines, title_size, title_line_height), (detail_lines, detail_size, detail_line_height)):
    for line in lines:
      draw_text_ui_style(line, width / 2, y, size, rl.WHITE, font=font, border_width=0,
                         shadow_offset=0, align='center_top', y_offset=0)
      y += line_height
    if lines is title_lines:
      y += gap
  return True
