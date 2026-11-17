"""Informational panel; existing alert renderers retain foreground priority."""
import time
import pyray as rl
from openpilot.common.pv5_upload_status import StatusReader, lines, show_onroad
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.widgets.label import gui_label

_reader = StatusReader()


def render_dashboard(rect, *, onroad=False, alert=False):
  now = time.monotonic()
  if onroad and not show_onroad(ui_state, now, alert):
    _reader.visible_until = 0.
    return
  if rect.width < 200 or rect.height < 120:
    return
  rows = lines(_reader.get(now), ui_state.sm, now)
  if onroad:
    width = min(680., rect.width - 24.)
    height = min(280., rect.height - 24.)
    rect = rl.Rectangle(rect.x + rect.width - width - 12., rect.y + rect.height - height - 12., width, height)
  padding = max(8., min(24., rect.width * .025))
  rl.draw_rectangle_rounded(rect, .06, 10, rl.Color(20, 29, 39, 235))
  row_height = (rect.height - 2 * padding) / (len(rows) + 1)
  size = max(13, min(34, int(row_height * .64), int((rect.width - 2 * padding) / 25)))
  for index, text in enumerate(['기기 · 전송 상태'] + rows):
    color = rl.Color(108, 210, 237, 255) if index == 0 else rl.WHITE if index < 6 else rl.Color(172, 183, 197, 255)
    gui_label(rl.Rectangle(rect.x + padding, rect.y + padding + index * row_height, rect.width - padding * 2, row_height),
              text, size, color)
