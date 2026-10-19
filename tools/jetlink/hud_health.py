"""Jetson-local temperature/module power; never substitute vehicle statistics."""
import math
import time
from dataclasses import replace

from host_health import STATUS
from openpilot.common.jetlink_status import _fresh
from cluster_battery import remote_panel
from cluster_config import CLUSTER_SCREEN_MODE_BATTERY
from cluster_renderer import (ClusterUiRenderer, DESIGN_WIDTH, DESIGN_HEIGHT,
                              WIFI_STATUS_CENTER_X, WIFI_STATUS_ICON_SIZE, TOP_CLOCK_CENTER_X, TOP_STATUS_CENTER_Y,
                              NAVI_LIVE_PANEL_X, NAVI_LIVE_PANEL_W, CAMERA_BACKGROUND_W,
                              SPEED_VALUE_CENTER_X, SPEED_VALUE_CENTER_Y, SPEED_VALUE_FONT_SIZE, rl, rl_color)

NORMAL = (100, 225, 170)
WARNING = (255, 205, 70)
ERROR = (255, 90, 90)
UNKNOWN = (190, 190, 190)


def vehicle_data_ready(snapshot):
  if snapshot is None:
    return False
  value = snapshot[1]
  return bool(value.get('alive', {}).get('carState') and value.get('valid', {}).get('carState')
              and value.get('events', {}).get('carState'))


def temperature_label(health):
  value = health.get('temp_c')
  if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not -20 <= value <= 150:
    return '--°C', UNKNOWN
  severity = health.get('thermal_severity', 'unknown')
  color = {'ok': NORMAL, 'warning': WARNING, 'error': ERROR}.get(severity, UNKNOWN)
  return f'{value:.1f}°C', color


def power_label(health):
  value = health.get('power_w')
  if (not isinstance(value, (int, float)) or isinstance(value, bool)
      or not math.isfinite(value) or not 0 <= value <= 200):
    return '-- W'
  return f'{value:.1f} W'


def health_banner(health, language):
  thermal = health.get('thermal_severity')
  if thermal in ('warning', 'error'):
    trip = health.get('thermal_trip') or {}
    temp, limit = trip.get('temp_c'), trip.get('limit_c')
    detail = ''
    if all(isinstance(v, (int, float)) and math.isfinite(v) for v in (temp, limit)):
      detail = f" · {temp:.1f}°C / {'기준' if language == 'ko' else 'limit'} {limit:.1f}°C"
    if language == 'ko':
      title = 'Jetson 과열' if thermal == 'error' else 'Jetson 온도 높음'
      message = f'{title}{detail} · 냉각 상태 확인'
    else:
      title = 'Jetson overheating' if thermal == 'error' else 'Jetson temperature high'
      message = f'{title}{detail} · Check cooling'
    return message, ERROR if thermal == 'error' else WARNING
  if health.get('severity', 'unknown') != 'ok':
    reason = health.get('reason') or ('온도·상태 정보 없음' if language == 'ko' else 'Temperature/health unavailable')
    return f'Jetson: {reason}', ERROR if health.get('severity') == 'error' else WARNING
  return '', NORMAL


class HealthRenderer(ClusterUiRenderer):
  def _host_metrics(self):
    from hud_runtime_stats import RuntimeFooterSampler
    if not hasattr(self, '_runtime_footer'):
      self._runtime_footer = RuntimeFooterSampler()
    return self._runtime_footer.sample()

  def _draw_runtime_line(self, segments, right_x, y, max_width, size, key):
    if not hasattr(self, '_runtime_line_layouts'):
      self._runtime_line_layouts = {}
    cached = self._runtime_line_layouts.get(key)
    if cached is None or cached[0] != segments:
      widths = [self._measure_text(text, size)[0] for text, _ in segments]
      while sum(widths) > max_width and size > 12:
        size -= 1
        widths = [self._measure_text(text, size)[0] for text, _ in segments]
      self._runtime_line_layouts[key] = segments, size, widths
    _, size, widths = self._runtime_line_layouts[key]
    width = sum(widths)
    x = right_x - 4 - width
    # Stable contrast for white/amber/red values in both light and dark themes.
    rl.draw_rectangle(int(x - 4), int(y - size * .7), int(width + 8), int(size * 1.4 + 2), rl_color((0, 0, 0), 205))
    for (text, color), advance in zip(segments, widths):
      self._draw_text(text, x, y, size, color)
      x += advance

  def _draw_cluster_core_usage(self, text, *, right_x=DESIGN_WIDTH):
    from hud_runtime_stats import footer_segments
    segments = footer_segments(self._system_stats.sample(), self._host_metrics(), self.language)
    self._draw_runtime_line(segments, right_x, 464, 760, 21, 'resources')

  def _draw_battery_panel(self, state):
    super()._draw_battery_panel(state)
    from hud_runtime_stats import inference_segments
    right = self._information_panel_x(NAVI_LIVE_PANEL_X) + NAVI_LIVE_PANEL_W - 20
    if right > DESIGN_WIDTH - 130:
      right = DESIGN_WIDTH - 135  # Leave the independent host-temperature badge clear.
    self._draw_runtime_line(inference_segments(self._host_metrics()), right, 32,
                            NAVI_LIVE_PANEL_W * .5 - 32, 18, 'infer')

  def _draw_battery_status(self, text, left, width, color):
    # Keep the complete availability message clear of the diagnostic footer.
    self._draw_text(self._ellipsize_text(text, 16, width), left, 55, 16, color)

  def close(self):
    footer = getattr(self, '_runtime_footer', None)
    if footer is not None:
      footer.close()
      del self._runtime_footer
    super().close()

  def render(self, state, signal_lights=None):
    from hud_protocol import read_snapshot
    from hud_bsd import warning_sides
    snapshot = read_snapshot()
    self._vehicle_data_current = vehicle_data_ready(snapshot)
    self._bsd_warning = warning_sides(state, snapshot, time.monotonic())
    self.host_health = _fresh(STATUS, time.monotonic())
    self.host_badge_drawn = False
    if getattr(self, 'screen_mode', None) == CLUSTER_SCREEN_MODE_BATTERY:
      state = replace(state, battery=remote_panel(snapshot, time.monotonic()))
    super().render(state, signal_lights)

  def _draw_speed_block(self, state, *, tpms_offset_x=0.):
    if getattr(self, '_vehicle_data_current', False):
      return super()._draw_speed_block(state, tpms_offset_x=tpms_offset_x)
    # A disconnected dashboard is visible, but a default/stale 0 is not a
    # measured stationary vehicle. Hide gear/cruise values in this block too.
    self._draw_speed_panel_bg()
    self._draw_text_with_stroke('--', SPEED_VALUE_CENTER_X, SPEED_VALUE_CENTER_Y,
                                SPEED_VALUE_FONT_SIZE, UNKNOWN, (0, 0, 0), 3, anchor='center')

  def _temperature_badge(self, x, y):
    label, color = temperature_label(self.host_health)
    rect = rl.Rectangle(x - 52, y - 17, 104, 60)
    rl.draw_rectangle_rounded(rect, .25, 8, rl_color((0, 0, 0), 190))
    rl.draw_rectangle_rounded_lines_ex(rect, .25, 8, 2., rl_color(color))
    self._draw_text(power_label(self.host_health), x, y, 19, (245, 245, 245), anchor='center')
    self._draw_text(label, x, y + 26, 20, color, anchor='center')
    self.host_badge_drawn = True

  def _draw_egpu_status(self, state):
    # The normal status row's transform also follows left/right panel swaps.
    x = WIFI_STATUS_CENTER_X + WIFI_STATUS_ICON_SIZE * .5 + 10 + 52
    self._temperature_badge(x, TOP_STATUS_CENTER_Y - 13)

  def _draw_center_clock(self, state, *, center_x=TOP_CLOCK_CENTER_X):
    if center_x == TOP_CLOCK_CENTER_X and state.center_clock_text:
      width, _ = self._measure_text(state.center_clock_text, 48)
      badge_right = WIFI_STATUS_CENTER_X + WIFI_STATUS_ICON_SIZE * .5 + 10 + 104
      center_x = max(TOP_CLOCK_CENTER_X + 100, badge_right + 16 + width * .5)
    super()._draw_center_clock(state, center_x=center_x)

  def _draw_alert_overlay(self, alert):
    rl.rl_push_matrix()
    rl.rl_scalef(self.width / DESIGN_WIDTH, self.height / DESIGN_HEIGHT, 1.)
    try:
      # Fixed geometry reuses the current frame; no new detector, CAN poll or
      # process. Existing vehicle alerts are still drawn last over this cue.
      left, right = getattr(self, '_bsd_warning', (False, False))
      panel = self._driving_panel_offset_design_x()
      for active, x in ((left, panel + 7), (right, panel + CAMERA_BACKGROUND_W - 21)):
        if active:
          rl.draw_rectangle(int(x - 7), 82, 28, int(DESIGN_HEIGHT - 100), rl_color((255, 30, 30), 80))
          rl.draw_rectangle(int(x), 82, 14, int(DESIGN_HEIGHT - 100), rl_color((255, 30, 30), 255))
      if not self.host_badge_drawn:
        # Full navigation/graph modes omit the driving status row.
        self._temperature_badge(DESIGN_WIDTH - 66, TOP_STATUS_CENTER_Y)
      message, color = health_banner(self.host_health, self.language)
      if not getattr(self, '_vehicle_data_current', True):
        waiting = '콤마 데이터 연결 대기 · 차량 정보 없음' if self.language == 'ko' else 'Waiting for comma data · Vehicle data unavailable'
        message = waiting + (' · ' + message if message else '')
        if color != ERROR:
          color = WARNING
      vehicle_alert = alert is not None and alert.size > 0 and (alert.text1 or alert.text2)
      if message and not vehicle_alert:
        rl.draw_rectangle(0, 0, int(DESIGN_WIDTH), 32, rl_color((0, 0, 0), 220))
        self._draw_text(self._ellipsize_text(message, 22, DESIGN_WIDTH - 40),
                        DESIGN_WIDTH / 2, 16, 22, color, anchor='center')
    finally:
      rl.rl_pop_matrix()
    # Vehicle alerts remain last and take precedence over the health strip.
    super()._draw_alert_overlay(alert)
