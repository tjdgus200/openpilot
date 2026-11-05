"""Jetson-local temperature/module power; never substitute vehicle statistics."""
import math
import time
from dataclasses import replace

from host_health import STATUS
from openpilot.common.jetlink_status import _fresh
from openpilot.common.performance_diagnostics import ENABLED as PERF_DIAGNOSTICS
from cluster_battery import remote_panel, StationaryBatteryView, valid_value
from cluster_config import CLUSTER_SCREEN_MODE_BATTERY
from cluster_renderer import (ClusterUiRenderer, DESIGN_WIDTH, DESIGN_HEIGHT, OPENPILOT_FONT_DIR,
                              TOP_STATUS_CENTER_Y,
                              NAVI_LIVE_PANEL_X, NAVI_LIVE_PANEL_W,
                              SIDE_GAUGE_LEFT_CENTER_X, SIDE_GAUGE_COLUMN_GAP, SIDE_GAUGE_BOTTOM,
                              SPEED_VALUE_CENTER_X, SPEED_VALUE_CENTER_Y, SPEED_VALUE_FONT_SIZE, rl, rl_color)

NORMAL = (100, 225, 170)
WARNING = (255, 205, 70)
TELEMETRY_ORANGE = (255, 165, 60)
ERROR = (255, 90, 90)
UNKNOWN = (190, 190, 190)
LANE_CENTER_STATUS = {
  'waiting': ('중앙 보정 대기', 'Center · Ready'),
  'inactive': ('중앙 · 제어 꺼짐', 'Center · Inactive'),
  'stopped': ('중앙 · 정차', 'Center · Stopped'),
  'laneMode': ('중앙 · 레인모드', 'Center · Lane mode'),
  'blindspot': ('중앙 · BSD 대기', 'Center · BSD hold'),
  'inputs': ('중앙 · 입력 대기', 'Center · Inputs'),
  'driver': ('중앙 · 운전자', 'Center · Driver'),
  'maneuver': ('중앙 · 방향 전환', 'Center · Maneuver'),
  'lead': ('중앙 · 앞차 감지', 'Center · Lead'),
  'radar': ('중앙 · 레이더', 'Center · Radar'),
  'laneQuality': ('중앙 · 차선 신뢰도', 'Center · Lane quality'),
  'geometry': ('중앙 · 경로 조건', 'Center · Geometry'),
  'checking': ('중앙 · 조건 확인', 'Center · Checking'),
  'centered': ('중앙 · 보정 불필요', 'Center · Aligned'),
}


class TripSocUsage:
  """One initial SOC per Jetson HUD session; no samples, timer or disk writes."""
  def __init__(self):
    self.initial = None

  def update(self, data, fresh_vehicle):
    value = data.value('soc_percent')
    if not fresh_vehicle or not valid_value('soc_percent', value):
      return None
    if self.initial is None:
      self.initial = value
    return round(self.initial - value, 1)


def trip_soc_label(used, language):
  if used is None:
    return '사용 --%' if language == 'ko' else 'Used --%'
  if language == 'ko':
    return f"{'사용' if used >= 0 else '증가'} {abs(used):.1f}%"
  return f"{'Used' if used >= 0 else 'Gain'} {abs(used):.1f}%"


def _bsd_corner_mesh(cx, cy, start, edge_width):
  # Square outside, rounded inside. Fan triangles share edges, so a single
  # translucent layer never darkens itself through overlapping primitives.
  radius = 72
  end = start + 90
  def outer(angle):
    return (cx + radius * round(math.cos(math.radians(angle))),
            cy + radius * round(math.sin(math.radians(angle))))
  first, last = outer(start), outer(end)
  corner = (first[0] + last[0] - cx, first[1] + last[1] - cy)
  inner = [(cx + (radius - edge_width) * math.cos(math.radians(start + i * 90 / 16)),
            cy + (radius - edge_width) * math.sin(math.radians(start + i * 90 / 16))) for i in range(17)]
  points = [corner, first, *inner, last]
  return rl.ffi.new('struct Vector2[]', points), len(points)


# Eight fixed 20-point meshes (~1.3 KiB), allocated once; no per-frame buffers.
_BSD_CORNER_MESHES = {(x, y, width): _bsd_corner_mesh(x, y, start, width)
                      for x, y, start in ((72, 72, 180), (72, 408, 90),
                                          (1848, 72, 270), (1848, 408, 0))
                      for width in (24, 48)}


def vehicle_data_ready(snapshot):
  if snapshot is None:
    return False
  value = snapshot[1]
  return bool(value.get('alive', {}).get('carState') and value.get('valid', {}).get('carState')
              and value.get('events', {}).get('carState'))


def vehicle_motion_fresh(snapshot, now):
  if not vehicle_data_ready(snapshot):
    return False
  try:
    received, record = snapshot
    ages = (now - received, record['sent'] - record['received']['carState'])
    return (all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in ages)
            and all(0 <= age <= .3 for age in ages) and sum(ages) <= .3)
  except (KeyError, TypeError, ValueError, OverflowError):
    return False


def temperature_label(health):
  value = health.get('temp_c')
  if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not -20 <= value <= 150:
    return '--°C', UNKNOWN
  color = NORMAL if value <= 70 else TELEMETRY_ORANGE if value <= 80 else ERROR
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
  # Renderer recreation for USB/FPS changes must not establish a new baseline.
  # A process restart (including a host reboot/runtime update) starts a session.
  _trip_soc = TripSocUsage()
  _telemetry_font = None

  def open(self, hidden=False):
    if self._window_open:
      return
    super().open(hidden=hidden)
    # Only this readout uses regular weight. Load its small glyph set once.
    chars = '0123456789.- WC°|'
    glyphs = rl.ffi.new('int[]', [ord(char) for char in chars])
    # pyray takes the address of CFFI arrays, so pass the int* API expects.
    font = rl.load_font_ex(str(OPENPILOT_FONT_DIR / 'Inter-Regular.ttf'), 52, rl.ffi.cast('int *', glyphs), len(chars))
    if font.texture.id > 0 and font.texture.id != rl.get_font_default().texture.id:
      self._telemetry_font = font
      rl.set_texture_filter(font.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)

  def usb_brightness_setting(self, state, setting):
    from hud_protocol import read_snapshot
    from hud_bsd import warning_sides, WarningBrightness
    if not hasattr(self, '_bsd_brightness'):
      self._bsd_brightness = WarningBrightness()
    return self._bsd_brightness.update(
      setting, warning_sides(state, read_snapshot(), time.monotonic()),
      (getattr(state, 'left_signal', False), getattr(state, 'right_signal', False)))

  def _host_metrics(self):
    from hud_runtime_stats import RuntimeFooterSampler
    if not hasattr(self, '_runtime_footer'):
      self._runtime_footer = RuntimeFooterSampler(timing_enabled=PERF_DIAGNOSTICS)
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
    for (text, color), advance in zip(segments, widths, strict=True):
      self._draw_text(text, x, y, size, color)
      x += advance

  def _draw_status_footer(self, state, *, include_core_usage=True):
    # Keep device usage visible alongside the trip report as well.
    super()._draw_status_footer(state, include_core_usage=True)

  def _draw_cluster_core_usage(self, text, *, right_x=DESIGN_WIDTH):
    from hud_runtime_stats import footer_segments
    segments = footer_segments(self._system_stats.sample(), self._host_metrics(), self.language)
    self._draw_runtime_line(segments, right_x, 464, 760, 21, 'resources')

  def _draw_battery_panel(self, state):
    super()._draw_battery_panel(state)
    used = getattr(self, '_trip_soc_used', None)
    right = self._information_panel_x(NAVI_LIVE_PANEL_X) + NAVI_LIVE_PANEL_W * .5 - 24
    self._draw_text(self._ellipsize_text(trip_soc_label(used, self.language), 28,
                                      NAVI_LIVE_PANEL_W * .5 - 48), right, 169, 28,
                    NORMAL if used is not None and used < 0 else self._current_theme().muted,
                    anchor='right')
    if not PERF_DIAGNOSTICS:
      return
    from hud_runtime_stats import inference_segments
    right = self._information_panel_x(NAVI_LIVE_PANEL_X) + NAVI_LIVE_PANEL_W - 20
    if right > DESIGN_WIDTH - 130:
      right = DESIGN_WIDTH - 135  # Leave the independent host-temperature badge clear.
    self._draw_runtime_line(inference_segments(self._host_metrics()), right, 32,
                            NAVI_LIVE_PANEL_W * .5 - 32, 18, 'infer')

  def _draw_battery_status(self, text, left, width, color):
    # Keep the complete availability message clear of the diagnostic footer.
    self._draw_text(self._ellipsize_text(text, 16, width), left, 55, 16, color)

  def _draw_steering_output_block(self, state):
    x = SIDE_GAUGE_LEFT_CENTER_X + (SIDE_GAUGE_COLUMN_GAP - 4) / 2
    self._draw_host_telemetry(x, 28)
    super()._draw_steering_output_block(state)
    status = state.pv5_lane_center_status
    fraction = state.pv5_lane_center_fraction if status == 'correcting' else 0.0
    active = status == 'correcting' and math.isfinite(fraction) and 0 < abs(fraction) <= 1.0
    ko = self.language == 'ko'
    if active:
      label = ('중앙 보정 ' if ko else 'Center ') + ('‹' if fraction < 0 else '›')
    elif status in LANE_CENTER_STATUS:
      label = LANE_CENTER_STATUS[status][0 if ko else 1]
    else:
      return
    y = SIDE_GAUGE_BOTTOM + 43
    color = (100, 200, 255) if active else UNKNOWN
    half_bar = 82
    rl.draw_rectangle(int(x - 101), int(y - 12), 202, 44, rl_color((5, 9, 12), 225))
    self._draw_text(self._ellipsize_text(label, 22, 190), x, y, 22, color, anchor='center')
    rl.draw_rectangle(int(x - half_bar), int(y + 17), half_bar * 2, 7, rl_color((52, 62, 74)))
    if active:
      width = max(1, round(half_bar * abs(fraction)))
      rl.draw_rectangle(int(x - width if fraction < 0 else x), int(y + 17), width, 7, rl_color(color))

  def close(self):
    footer = getattr(self, '_runtime_footer', None)
    if footer is not None:
      footer.close()
      del self._runtime_footer
    if self._telemetry_font is not None:
      rl.unload_font(self._telemetry_font)
      self._telemetry_font = None
    super().close()

  def render(self, state, signal_lights=None):
    from hud_protocol import read_snapshot
    from hud_bsd import warning_sides, WarningPulse
    snapshot = read_snapshot()
    self._vehicle_data_current = vehicle_data_ready(snapshot)
    now = time.monotonic()
    # Keep the existing low-priority sampler alive in layouts that omit its
    # visible footer too; quality observations must not depend on screen mode.
    from hud_quality import RUN_PROFILE
    if PERF_DIAGNOSTICS and RUN_PROFILE is not None:
      self._host_metrics()
    self._bsd_warning = warning_sides(state, snapshot, now)
    if not hasattr(self, '_bsd_pulse'):
      self._bsd_pulse = WarningPulse()
    self._bsd_alpha = self._bsd_pulse.update(self._bsd_warning,
                                            (getattr(state, 'left_signal', False),
                                             getattr(state, 'right_signal', False)), now=now)
    self.host_health = _fresh(STATUS, time.monotonic())
    self.host_telemetry_drawn = False
    battery = remote_panel(snapshot, time.monotonic())
    vehicle_current = self._vehicle_data_current and vehicle_motion_fresh(snapshot, now)
    self._trip_soc_used = self._trip_soc.update(battery, vehicle_current)
    if getattr(self, 'screen_mode', None) == CLUSTER_SCREEN_MODE_BATTERY:
      state = replace(state, battery=battery)
    if not hasattr(self, '_battery_view'):
      self._battery_view = StationaryBatteryView()
    battery_screen = getattr(self, 'screen_mode', None) == CLUSTER_SCREEN_MODE_BATTERY
    self._charging_dashboard_active = self._battery_view.update(
      getattr(state, 'battery', None), getattr(state, 'gear_text', ''), getattr(state, 'speed_kph', None),
      battery_screen and vehicle_current, now)
    super().render(state, signal_lights)

  def _draw_speed_block(self, state, *, tpms_offset_x=0.):
    if getattr(self, '_vehicle_data_current', False):
      return super()._draw_speed_block(state, tpms_offset_x=tpms_offset_x)
    # A disconnected dashboard is visible, but a default/stale 0 is not a
    # measured stationary vehicle. Hide gear/cruise values in this block too.
    self._draw_speed_panel_bg()
    self._draw_text_with_stroke('--', SPEED_VALUE_CENTER_X, SPEED_VALUE_CENTER_Y,
                                SPEED_VALUE_FONT_SIZE, UNKNOWN, (0, 0, 0), 3, anchor='center')

  def _draw_host_telemetry(self, x, y):
    temperature, temperature_color = temperature_label(self.host_health)
    power = power_label(self.host_health)
    power_color = UNKNOWN
    if power != '-- W':
      watts = self.host_health['power_w']
      power_color = NORMAL if watts <= 20 else TELEMETRY_ORANGE if watts < 30 else ERROR
    parts = ((power, power_color), (' | ', (245, 245, 245)), (temperature, temperature_color))
    font = self._telemetry_font if self._telemetry_font is not None else rl.get_font_default()
    measures = [self._measure_text(text, 26, 1, font) for text, _ in parts]
    size = 26 * min(1.0, 194 / max(1.0, sum(width for width, _ in measures)))
    if size < 26:
      measures = [self._measure_text(text, size, 1, font) for text, _ in parts]
    left = x - sum(width for width, _ in measures) * 0.5
    for (text, color), (width, height) in zip(parts, measures, strict=True):
      rl.draw_text_ex(font, text, rl.Vector2(left, y - height * 0.5), size, 1, rl_color(color))
      left += width
    self.host_telemetry_drawn = True

  def _draw_egpu_status(self, state):
    # Jetson telemetry is drawn with the side gauges.
    pass

  def _draw_alert_overlay(self, alert):
    left, right = getattr(self, '_bsd_warning', (False, False))
    if left or right:
      # Finish the existing HUD batch before the gradient's native quad changes
      # drawing state; otherwise preceding panel text can be clipped. No GPU wait.
      rl.rl_draw_render_batch_active()
    rl.rl_push_matrix()
    rl.rl_scalef(self.width / DESIGN_WIDTH, self.height / DESIGN_HEIGHT, 1.)
    try:
      # Fixed geometry reuses the current frame; no new detector, CAN poll or
      # process. Existing vehicle alerts are still drawn last over this cue.
      from hud_bsd import GRADIENT_ALPHA
      alphas = getattr(self, '_bsd_alpha', (255, 255))
      # Physical screen sides stay left/right even when the panels are swapped.
      for side, active in enumerate((left, right)):
        if active:
          # One constant, translucent native quad per side. No image buffers,
          # shader, thread or timer. The center third remains untinted.
          # The same-side turn request pulses only the red bracket.
          shade, clear = rl_color((255, 30, 30), GRADIENT_ALPHA), rl_color((255, 30, 30), 0)
          width = int(DESIGN_WIDTH / 3)
          rl.draw_rectangle_gradient_h(0 if side == 0 else int(DESIGN_WIDTH) - width,
                                       0, width, int(DESIGN_HEIGHT),
                                       shade if side == 0 else clear, clear if side == 0 else shade)
          alpha = alphas[side]
          if not alpha:
            continue
          # Three-sided brackets surround only the physical outer third.
          # Filled square outer corners keep the rounded inner wall/rail join.
          radius = 72
          corner_x = radius if side == 0 else int(DESIGN_WIDTH) - radius
          for edge_width, opacity in ((48, int(alpha * .3)), (24, alpha)):
            x = 0 if side == 0 else int(DESIGN_WIDTH) - edge_width
            color = rl_color((255, 30, 30), opacity)
            rl.draw_rectangle(x, radius, edge_width, int(DESIGN_HEIGHT) - 2 * radius, color)
            for corner_y in (radius, int(DESIGN_HEIGHT) - radius):
              points, count = _BSD_CORNER_MESHES[corner_x, corner_y, edge_width]
              rl.draw_triangle_fan(rl.ffi.addressof(points, 0), count, color)
            horizontal_x = radius if side == 0 else int(DESIGN_WIDTH) - width
            for y in (0, int(DESIGN_HEIGHT) - edge_width):
              # Fade both rails to transparent at the inner third boundary,
              # keeping the side wall bright without a hard-cut inner end.
              rl.draw_rectangle_gradient_h(horizontal_x, y, width - radius, edge_width,
                                           color if side == 0 else clear,
                                           clear if side == 0 else color)
      if not self.host_telemetry_drawn:
        # Layouts without side gauges still show the telemetry as text.
        self._draw_host_telemetry(DESIGN_WIDTH - 100, TOP_STATUS_CENTER_Y)
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
