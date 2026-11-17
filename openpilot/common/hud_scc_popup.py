"""Presentation only for the already selected, nonurgent SCC information alert."""

import pyray as rl


def is_scc_info_popup(alert):
  # Never reinterpret urgent SCC, FCW, driver warnings, or timeout alerts.
  return (alert is not None and alert.alert_type == 'pv5SccDecelInfo/warning'
          and alert.status == 0 and alert.size == 1)


def draw_scc_info_popup(alert, viewport, draw_text):
  """Opaque, centered popup; no timer, queue, fade-in, or stale-alert hold."""
  if not is_scc_info_popup(alert):
    return False
  width = viewport.width * 0.82
  height = viewport.height * 0.18
  box = rl.Rectangle(viewport.x + (viewport.width - width) / 2,
                     viewport.y + viewport.height * 0.30, width, height)
  rl.draw_rectangle_rounded(box, 0.24, 8, rl.Color(21, 29, 39, 245))
  rl.draw_rectangle_rounded_lines_ex(box, 0.24, 8, max(2., height * 0.018), rl.Color(90, 205, 230, 255))
  draw_text(box, alert.text1)
  return True
