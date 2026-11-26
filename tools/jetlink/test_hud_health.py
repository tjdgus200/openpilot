import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/carrot/cluster')]
import hud_health as hud
from host_health import thermal_health, storage_health
from cluster_battery import BatteryPanelData, packet_from_metrics, panel_from_wire


def test_wide_class_is_drawn_for_the_exact_measured_front_id(tmp_path, monkeypatch):
  import json
  from dataclasses import replace
  import hud_protocol
  from hud_track_classes import RadarRows, TrackClassReader
  from cluster_live import standby_state
  from cluster_models import RadarPoint
  from cluster_route_replay import live_track_to_radar_point
  from cluster_scene import vehicle_box
  import openpilot.common.indicator_display as indicator

  renderer = hud.HealthRenderer(screen_mode=8, language='ko')
  renderer._track_tiles = {0: (object(),) * 16}
  now = 100.
  stamp = 34000
  record = dict(sent=now, received={'carState': now, 'liveTracks': now}, events={'carState':'x', 'liveTracks':'x'},
                valid={'carState': True, 'liveTracks': True},
                alive={'carState': True, 'liveTracks': True}, mono={'liveTracks': stamp})
  renderer._track_label_path = tmp_path / 'labels.json'
  renderer._track_label_path.write_text(json.dumps(dict(version=1, lens='wide', radar_ns=stamp, published_monotonic_ns=int(now*1e9),
    labels=[dict(track_id=34,class_id=0,confidence=.83,point=[25.,0.,0.],expires_monotonic_ns=int((now+.1)*1e9))])))
  monkeypatch.setattr(hud_protocol, 'read_snapshot', lambda: (now, record))
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now)
  monkeypatch.setattr(hud.time, 'monotonic_ns', lambda: int(now*1e9))
  monkeypatch.setattr(hud, '_fresh', lambda *_: {})
  monkeypatch.setattr(indicator, 'draw', lambda *_: None)
  monkeypatch.setattr(hud.ClusterUiRenderer, 'render', lambda r, state, *_: r._render_world(state))
  monkeypatch.setattr(hud.ClusterUiRenderer, '_render_world', lambda *_: None)
  front = NS(trackId=34, dRel=25., yRel=0., vRel=0., measured=True, radarSource='frontRadar')
  radar = NS(points=[front], radarTrackFlipped=False, errors=NS(to_dict=lambda: {}))
  event = NS(logMonoTime=stamp, valid=True, liveTracks=radar)
  observations = RadarRows()
  observations.observe(event)
  renderer._label_reader = TrackClassReader(renderer._track_label_path, observations)
  point = live_track_to_radar_point(front, 0, 0.)
  state = replace(standby_state(), onroad=True, radar_points=(point,))
  renderer.render(state)
  assert isinstance(point, RadarPoint) and point.front_track_id == 34
  assert renderer._track_icon_tile(point, 0., 25.) is renderer._track_tiles[0][0]
  box = vehicle_box(0, 25., 0., 3.5, (255,255,255), False, front_track_id=34)
  assert renderer._track_icon_tile(box, 0., 25.) is renderer._track_tiles[0][0]
  assert renderer._track_icon_tile(replace(box, front_track_id=43), 0., 25.) is None
  state = replace(state, radar_points=(), detected_vehicles=())
  renderer.render(state)
  assert [p.front_track_id for p in renderer.hud_labeled_radar_points] == [34]
  cached = renderer.hud_labeled_radar_points
  renderer.render(state)
  assert renderer.hud_labeled_radar_points is cached
  # The packet reader can advance before the HUD subscriber decodes liveTracks.
  now += .12
  stamp += 120_000_000
  record.update(sent=now, received={'carState': now, 'liveTracks': now}, mono={'liveTracks': stamp})
  renderer.render(state)
  assert not renderer.hud_labeled_radar_points
  event.logMonoTime = stamp
  observations.observe(event)
  renderer.render(state)
  assert renderer.hud_track_classes == {34: 0}
  radar.points.append(front)
  observations.observe(event)
  renderer.render(state)
  assert not renderer.hud_labeled_radar_points and not renderer.hud_track_classes
  radar.points.pop()
  front.measured = False
  assert live_track_to_radar_point(front, 0, 0.).front_track_id is None
  record['mono']['liveTracks'] += 1
  renderer.render(state)
  assert renderer._track_icon_tile(point, 0., 25.) is None


def test_six_track_models_share_the_detector_ids_and_release_on_partial_load(monkeypatch):
  import cluster_track_models as models
  from cluster_renderer import CLUSTER_DIR
  from hud_track_classes import HUD_CLASSES
  assert tuple(style[0] for style in models.STYLES) == HUD_CLASSES
  supplied, released = [], []
  def upload(entry, palette, dimensions):
    assert len(entry['vertices']) >= 4 and len(entry['faces']) >= 4 and len(palette) > 1 and min(dimensions) > 0
    if len(supplied) == 3:
      raise RuntimeError('model upload failed')
    supplied.append(object())
    return supplied[-1]
  monkeypatch.setattr(models, '_upload_model', upload)
  monkeypatch.setattr(models.rl, 'unload_model', released.append)
  with pytest.raises(RuntimeError, match='model upload failed'):
    models.load_track_models(CLUSTER_DIR / 'assets/models/track_classes.json')
  assert len(released) == 3 and released == supplied


@pytest.mark.parametrize('layout', (0, 1))
def test_park_adds_partial_tpms_and_trip_in_unused_heatmap_row(monkeypatch, layout):
  from cluster_models import TpmsInfo, TripReportState
  renderer = hud.HealthRenderer(screen_mode=8, panel_layout=layout, language='en')
  renderer._pv5_hud_gear = 'P'
  calls = []
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_charging_detail', lambda *_: calls.append('heatmap'))
  renderer._draw_text = lambda text, x, y, *_: calls.append((text, x, y))
  state = NS(tpms=TpmsInfo(36., None, 34., 33.), trip_report=TripReportState(distance_m=1250.))
  renderer._draw_charging_detail(state)
  x = renderer._driving_panel_offset_design_x()
  assert calls == ['heatmap', ('P   TPMS    Front L/R 36 / --    Rear L/R 34 / 33 psi', x + 24, 188),
                   ('Trip  1.25 km', x + 664, 188)]
  for gear in ('D', 'N', None):
    renderer._pv5_hud_gear = gear
    calls.clear()
    renderer._draw_charging_detail(state)
    assert calls == ['heatmap']


def test_reverse_replaces_forward_view_keeps_alert_order_and_releases_on_snapshot_loss(monkeypatch):
  from dataclasses import replace
  from cluster_live import standby_state
  from cluster_models import ClusterAlert
  import hud_protocol
  import openpilot.common.indicator_display as indicator

  renderer = hud.HealthRenderer(screen_mode=8)
  now = 100.
  record = {'sent': now, 'received': {'carState': now}, 'events': {'carState': 'present'},
            'valid': {'carState': True}, 'alive': {'carState': True}}
  monkeypatch.setattr(hud_protocol, 'read_snapshot', lambda: (now, record))
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now)
  monkeypatch.setattr(hud, '_fresh', lambda *_: {})
  monkeypatch.setattr(indicator, 'draw', lambda *_: None)
  calls = []
  renderer._clear_world = lambda: calls.append('clear')
  renderer._draw_turn_signal = lambda side, light, **kw: calls.append((side, light))
  renderer._draw_text = lambda text, *a, **kw: calls.append(text)
  renderer._draw_driving_popup = lambda *_: calls.append('popup')
  renderer._draw_status_footer = lambda *_: calls.append('footer')
  renderer._draw_alert_overlay = lambda alert: calls.append(alert)
  for name in ('rl_push_matrix', 'rl_pop_matrix', 'rl_scalef', 'draw_rectangle'):
    monkeypatch.setattr(hud.rl, name, lambda *_: None)
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_hud', lambda *_: calls.append('ordinary hud'))
  monkeypatch.setattr(hud.ClusterUiRenderer, '_render_world', lambda *_: calls.append('ordinary world'))
  alert = ClusterAlert('Urgent', size=3, status=2)
  state = replace(standby_state(), onroad=True, gear_text='R', pv5_hud_gear='R', alert=alert)
  renderer.render(state, signal_lights=(True, False))
  assert calls[0] == 'clear' and 'R' in calls and ('left', True) in calls
  assert calls[-3:] == ['popup', 'footer', alert] and 'ordinary hud' not in calls
  record['received']['carState'] -= .31
  calls.clear()
  renderer.render(state, signal_lights=(False, False))
  assert calls == ['ordinary world', 'ordinary hud', alert]
  record['received']['carState'] = now
  for gear in ('D', 'N'):
    calls.clear()
    renderer.render(replace(state, gear_text=gear, pv5_hud_gear=None), signal_lights=(False, False))
    assert calls == ['ordinary world', 'ordinary hud', alert]


def test_trip_soc_uses_first_valid_observation_and_survives_missing_data():
  trip = hud.TripSocUsage()
  def panel(value, now=100.):
    return panel_from_wire(packet_from_metrics({'soc_percent': (value, 100.)}, now))
  assert trip.update(panel(90.), False) is None and trip.initial is None
  assert trip.update(BatteryPanelData(), True) is None and trip.initial is None
  assert trip.update(panel(77.), True) == 0.
  assert trip.update(panel(74.5), True) == 2.5
  assert trip.update(panel(74.5, 160.01), True) is None
  assert trip.update(BatteryPanelData(), False) is None
  assert trip.initial == 77.
  assert trip.update(panel(73.), True) == 4.
  assert trip.update(panel(78.), True) == -1.  # Charging/regen is not clamped to consumption.
  assert hud.trip_soc_label(2.5, 'ko') == '사용 2.5%'
  assert hud.trip_soc_label(-1., 'ko') == '증가 1.0%'
  assert hud.trip_soc_label(None, 'en') == 'Used --%'


@pytest.mark.parametrize('value', [True, '77', -1., 101., float('nan'), float('inf')])
def test_invalid_trip_soc_cannot_set_initial_value(value):
  trip = hud.TripSocUsage()
  assert trip.update(BatteryPanelData((('soc_percent', value, 0.),)), True) is None
  assert trip.initial is None


def test_trip_soc_zero_is_valid_and_renderer_recreation_preserves_session(monkeypatch):
  trip = hud.TripSocUsage()
  monkeypatch.setattr(hud.HealthRenderer, '_trip_soc', trip)
  first, second = object.__new__(hud.HealthRenderer), object.__new__(hud.HealthRenderer)
  assert first._trip_soc.update(BatteryPanelData((('soc_percent', 0., 0.),)), True) == 0.
  assert second._trip_soc.update(BatteryPanelData((('soc_percent', 1., 0.),)), True) == -1.
  assert trip.initial == 0.


@pytest.mark.parametrize('language', ['ko', 'en'])
def test_trip_soc_is_larger_diagonally_below_soc_and_preserves_base_panel(monkeypatch, language):
  renderer = hud.HealthRenderer()
  renderer.language, renderer._trip_soc_used = language, 2.5
  texts, base = [], []
  renderer._draw_text = lambda *args, **kw: texts.append((args, kw))
  renderer._ellipsize_text = lambda text, *args: text
  renderer._draw_runtime_line = lambda *args: None
  renderer._host_metrics = dict
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_battery_panel', lambda *args: base.append(True))
  renderer._draw_battery_panel(NS())
  assert base == [True]
  args, kw = texts[0]
  assert args[0] == hud.trip_soc_label(2.5, language)
  assert args[1] == renderer._information_panel_x(hud.NAVI_LIVE_PANEL_X) + hud.NAVI_LIVE_PANEL_W*.30
  assert args[2:4] == (169, 28) and kw['anchor'] == 'right'


def test_trip_soc_renderer_uses_only_valid_fresh_remote_data(monkeypatch):
  from dataclasses import replace
  import hud_protocol
  from cluster_live import standby_state
  trip, clock, connected = hud.TripSocUsage(), [100.], [True]
  monkeypatch.setattr(hud.HealthRenderer, '_trip_soc', trip)
  soc = [77.]
  def snapshot():
    if not connected[0]:
      return None
    return clock[0], {'sent': clock[0], 'received': {'carState': clock[0]-.01},
                      'events': {'carState': 'present'}, 'alive': {'carState': True},
                      'valid': {'carState': True},
                      'battery_hud': packet_from_metrics({'soc_percent': (soc[0], clock[0])}, clock[0])}
  monkeypatch.setattr(hud_protocol, 'read_snapshot', snapshot)
  monkeypatch.setattr(hud.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(hud, '_fresh', lambda *args: {})
  monkeypatch.setattr(hud.ClusterUiRenderer, 'render', lambda *args: None)
  renderer = hud.HealthRenderer(screen_mode=8)
  state = replace(standby_state(), gear_text='D', speed_kph=10.)
  renderer.render(state)
  assert renderer._trip_soc_used == 0.
  connected[0], clock[0] = False, 101.
  renderer.render(state)
  assert renderer._trip_soc_used is None and trip.initial == 77.
  connected[0], soc[0], clock[0] = True, 75., 102.
  renderer.render(state)
  assert renderer._trip_soc_used == 2.


def sensor(root, index, temp, limit, cooling='cpu-balanced'):
  zone = root / f'thermal_zone{index}'
  zone.mkdir()
  (zone / 'type').write_text(f'cpu{index}-thermal')
  (zone / 'temp').write_text(str(temp * 1000))
  (zone / 'trip_point_0_type').write_text('passive')
  (zone / 'trip_point_0_temp').write_text(str(limit * 1000))
  (zone / 'cdev0_trip_point').write_text('0')
  (zone / 'cdev0').mkdir()
  (zone / 'cdev0/type').write_text(cooling)
  return zone


@pytest.mark.parametrize('temp,severity', [(93, 'ok'), (94, 'warning'), (99, 'error')])
def test_real_trip_drives_bilingual_warning(tmp_path, temp, severity):
  sensor(tmp_path, 0, temp, 99)
  health = thermal_health(tmp_path)
  assert hud.temperature_label(health) == (f'{temp:.1f}°C', hud.ERROR)
  assert health['thermal_severity'] == severity
  for language, word in [('ko', '냉각'), ('en', 'cooling')]:
    message, _ = hud.health_banner(health, language)
    assert (word in message) == (severity != 'ok')
    if severity != 'ok':
      assert f'{temp:.1f}°C' in message and '99.0°C' in message and 'passive' not in message


def test_surface_notification_is_not_overheat_and_each_zone_uses_own_trip(tmp_path):
  sensor(tmp_path, 0, 75, 70, 'hot-surface-alert')
  assert thermal_health(tmp_path)['thermal_severity'] == 'ok'
  sensor(tmp_path, 1, 87, 92)
  health = thermal_health(tmp_path)
  assert health['thermal_severity'] == 'warning'
  assert 'cpu1-thermal' in health['thermal_reason'] and 'cpu0-thermal' not in health['thermal_reason']


def test_storage_fault_is_not_labelled_overheating(tmp_path):
  sensor(tmp_path, 0, 65, 99)
  health = thermal_health(tmp_path)
  marker = tmp_path / 'marker'
  marker.touch()
  storage_health(health, tmp_path / 'missing', marker)
  assert hud.temperature_label(health) == ('65.0°C', hud.NORMAL)
  message, color = hud.health_banner(health, 'en')
  assert 'Storage' in message and 'overheating' not in message and color == hud.ERROR


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -256, 200, '99', True])
def test_missing_or_invalid_temperature_does_not_look_current(value):
  assert hud.temperature_label({'temp_c': value}) == ('--°C', hud.UNKNOWN)


def test_freshness_and_vehicle_alert_precedence(monkeypatch, tmp_path):
  import json

  path = tmp_path / 'health'
  path.write_text(json.dumps({'updated': 10.0, 'temp_c': 99.0, 'thermal_severity': 'error', 'thermal_reason': 'cpu 99C'}))
  monkeypatch.setattr(hud, 'STATUS', path)
  monkeypatch.setattr(hud.time, 'monotonic', lambda: 13.0)
  monkeypatch.setattr(hud.ClusterUiRenderer, 'render', lambda *a: None)
  renderer = object.__new__(hud.HealthRenderer)
  renderer.render(NS())
  assert renderer.host_health == {}
  renderer.width, renderer.height, renderer.language = 1920, 480, 'ko'
  renderer.host_health = {'thermal_severity': 'error', 'thermal_reason': 'cpu 99C'}
  renderer.host_telemetry_drawn = True
  calls = []
  for name in ('rl_push_matrix', 'rl_pop_matrix', 'rl_scalef'):
    monkeypatch.setattr(hud.rl, name, lambda *a: None)
  monkeypatch.setattr(hud.rl, 'draw_rectangle', lambda *a: calls.append('strip'))
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_alert_overlay', lambda *a: calls.append('vehicle'))
  renderer._draw_text = lambda *a, **kw: calls.append('text')
  renderer._ellipsize_text = lambda text, *a: text
  renderer._draw_alert_overlay(NS(size=3, text1='TAKE OVER', text2=''))
  assert calls == ['vehicle']
  calls.clear()
  renderer._draw_alert_overlay(None)
  assert calls == ['strip', 'text', 'vehicle']
  calls.clear()
  renderer.host_health = {'severity': 'ok'}
  renderer._vehicle_data_current = True
  renderer._host_metrics = lambda: {}
  renderer._draw_runtime_line = lambda *a: calls.append(a[-1])
  renderer._draw_alert_overlay(None)
  assert calls == ['infer', 'comma-infer', 'vehicle']


@pytest.mark.parametrize(
  'value,expected',
  [
    (0, '0.0 W'),
    (7.67296, '7.7 W'),
    (25, '25.0 W'),
    (None, '-- W'),
    (True, '-- W'),
    (-1, '-- W'),
    (float('nan'), '-- W'),
    (float('inf'), '-- W'),
    (201, '-- W'),
  ],
)
def test_power_label_has_units_and_rejects_unavailable_or_invalid_values(value, expected):
  assert hud.power_label({'power_w': value}) == expected


def test_expired_health_does_not_keep_old_power(tmp_path):
  import json

  path = tmp_path / 'health.json'
  path.write_text(json.dumps({'updated': 10.0, 'power_w': 12.4}))
  assert hud.power_label(hud._fresh(path, 10.5)) == '12.4 W'
  assert hud.power_label(hud._fresh(path, 14.0)) == '-- W'


def test_regular_telemetry_font_is_loaded_once_and_released(monkeypatch):
  renderer = object.__new__(hud.HealthRenderer)
  renderer._window_open = False
  font = NS(texture=NS(id=2))
  loaded, unloaded = [], []
  monkeypatch.setattr(hud.ClusterUiRenderer, 'open', lambda self, hidden=False: setattr(self, '_window_open', True))
  monkeypatch.setattr(hud.ClusterUiRenderer, 'close', lambda self: setattr(self, '_window_open', False))
  monkeypatch.setattr(hud.rl, 'get_font_default', lambda: NS(texture=NS(id=1)))
  def load(path, size, glyphs, count):
    assert hud.rl.ffi.typeof(glyphs) == hud.rl.ffi.typeof('int *')
    loaded.append((Path(path).name, size, ''.join(chr(glyphs[i]) for i in range(count))))
    return font
  monkeypatch.setattr(hud.rl, 'load_font_ex', load)
  monkeypatch.setattr(hud.rl, 'set_texture_filter', lambda *_: None)
  monkeypatch.setattr(hud.rl, 'unload_font', unloaded.append)
  renderer.open(hidden=True)
  renderer.open(hidden=True)
  assert loaded == [('Inter-Regular.ttf', 52, '0123456789.- WC°|')]
  assert renderer._telemetry_font is font
  renderer.close()
  renderer.close()
  assert unloaded == [font]
  assert renderer._telemetry_font is None


@pytest.mark.parametrize('watts,temp,power_color,temp_color', [
  (20, 70, hud.NORMAL, hud.NORMAL),
  (20.1, 80, hud.TELEMETRY_ORANGE, hud.TELEMETRY_ORANGE),
  (30, 80.1, hud.ERROR, hud.ERROR),
  (12.4, 81, hud.NORMAL, hud.ERROR),
  (None, None, hud.UNKNOWN, hud.UNKNOWN),
])
def test_host_telemetry_is_one_line_above_side_gauges(monkeypatch, watts, temp, power_color, temp_color):
  renderer = object.__new__(hud.HealthRenderer)
  renderer.host_health = {'power_w': watts, 'temp_c': temp, 'thermal_severity': 'ok'}
  calls = []
  font = renderer._telemetry_font = object()
  def measure(text, size, spacing, selected_font):
    assert selected_font is font
    return 60, 26
  renderer._measure_text = measure
  monkeypatch.setattr(hud.rl, 'draw_text_ex', lambda *args: calls.append(args))
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_steering_output_block', lambda self, state: None)
  renderer._draw_egpu_status(NS())
  assert calls == []
  renderer._draw_steering_output_block(NS(pv5_lane_center_status='off'))
  x = hud.SIDE_GAUGE_LEFT_CENTER_X + (hud.SIDE_GAUGE_COLUMN_GAP - 4) / 2
  assert [args[1] for args in calls] == [
    hud.power_label(renderer.host_health), ' | ', hud.temperature_label(renderer.host_health)[0],
  ]
  assert [args[2].x for args in calls] == [x - 90, x - 30, x + 30]
  assert [(args[5].r, args[5].g, args[5].b) for args in calls] == [power_color, (245, 245, 245), temp_color]
  assert all(args[0] is font and args[2].y == 15 and args[3:5] == (26, 1) for args in calls)
  assert renderer.host_telemetry_drawn


@pytest.mark.parametrize(
  'snapshot',
  [
    None,
    (1.0, {}),
    (1.0, {'alive': {'carState': True}, 'valid': {'carState': False}, 'events': {'carState': 'encoded'}}),
    (1.0, {'alive': {'carState': False}, 'valid': {'carState': True}, 'events': {'carState': 'encoded'}}),
    (1.0, {'alive': {'carState': True}, 'valid': {'carState': True}, 'events': {}}),
  ],
)
def test_waiting_dashboard_does_not_present_default_or_stale_speed(snapshot):
  renderer = object.__new__(hud.HealthRenderer)
  renderer._vehicle_data_current = hud.vehicle_data_ready(snapshot)
  calls = []
  renderer._draw_speed_panel_bg = lambda: calls.append('background')
  renderer._draw_text_with_stroke = lambda text, *a, **kw: calls.append(text)
  renderer._draw_speed_block(NS(speed_kph=0.0))
  assert calls == ['background', '--']


def test_valid_vehicle_data_restores_normal_speed_renderer(monkeypatch):
  snapshot = (1.0, {'alive': {'carState': True}, 'valid': {'carState': True}, 'events': {'carState': 'encoded'}})
  renderer = object.__new__(hud.HealthRenderer)
  renderer._vehicle_data_current = hud.vehicle_data_ready(snapshot)
  calls = []
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_speed_block', lambda *a, **kw: calls.append(kw))
  renderer._draw_speed_block(NS(), tpms_offset_x=7.0)
  assert calls == [{'tpms_offset_x': 7.0}]

def test_resource_footer_stays_visible_when_performance_diagnostics_are_off(monkeypatch):
  import hud_runtime_stats

  monkeypatch.setattr(hud, 'PERF_DIAGNOSTICS', False)
  options = []
  monkeypatch.setattr(hud_runtime_stats, 'RuntimeFooterSampler', lambda **kw: (
    options.append(kw) or NS(sample=lambda: {'cpu_percent': 8., 'memory_percent': 27., 'gpu_percent': 54.})))
  renderer = object.__new__(hud.HealthRenderer)
  renderer.language = 'ko'
  renderer._system_stats = NS(sample=lambda: NS(cpu_used_percent=12., memory_used_percent=49.))
  lines = []
  renderer._draw_runtime_line = lambda segments, *_: lines.append(''.join(text for text, _ in segments))
  renderer._draw_cluster_core_usage(None)
  assert options == [{'timing_enabled': True, 'compare_quality': False}]
  assert lines == ['COMMA CPU:12% MEM:49% | ORIN CPU:8% MEM:27% GPU:54%']
