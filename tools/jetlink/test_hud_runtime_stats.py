import base64
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/jetlink'), str(ROOT / 'third_party/jetlink'), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]
from hud_runtime_stats import (
  gpu_percent, footer_lines, inference_segments, model_call_sample, model_diagnostics_sample, valid_timing,
  RuntimeFooterSampler, metric_color, NORMAL, CAUTION, HIGH, UNKNOWN,
)
from jetlink.server.session import FrameStats
from jetlink.server.control import ControlServer


def timing():
  return {
    'window_s': 10.0,
    'count': 200,
    'last_frame_age_s': 0.1,
    'infer_ms': {'mean': 25.0, 'p99': 28.0, 'max': 31.0},
    'server_ms': {'mean': 27.0, 'p99': 30.0, 'max': 33.0},
  }


@pytest.mark.parametrize('raw,expected', [('0', 0.0), ('1000', 100.0), ('537', 53.7), ('-1', None), ('1001', None), ('nan', None), ('bad', None)])
def test_gpu_load_units_and_invalid_values(tmp_path, raw, expected):
  path = tmp_path / 'load'
  path.write_text(raw)
  assert gpu_percent((tmp_path / 'missing', path)) == expected


def test_footer_separates_hosts_and_names_timing_scope():
  lines = footer_lines(
    NS(cpu_used_percent=12.0, memory_used_percent=49.0), {'cpu_percent': 8.0, 'memory_percent': 27.0, 'gpu_percent': 53.7, 'timing': timing()}
  )
  assert len(lines) == 2
  assert 'COMMA CPU:12% MEM:49%' in lines[0]
  assert 'ORIN CPU:8% MEM:27% GPU:54%' in lines[0]
  assert '10s' in lines[1] and 'AVG:25.0' in lines[1] and 'P99:28.0' in lines[1] and 'MAX:31.0' in lines[1]


def test_resource_colors_are_display_only_and_gpu_load_is_not_a_fault():
  assert metric_color(None, 'cpu') == UNKNOWN
  assert metric_color(79, 'cpu') == NORMAL and metric_color(80, 'cpu') == CAUTION
  assert metric_color(89, 'cpu') == CAUTION and metric_color(90, 'cpu') == HIGH
  assert metric_color(79, 'memory') == NORMAL and metric_color(80, 'memory') == CAUTION
  assert metric_color(89, 'memory') == CAUTION and metric_color(90, 'memory') == HIGH
  assert metric_color(35, 'infer') == CAUTION and metric_color(50, 'infer') == HIGH
  assert metric_color(100, 'gpu') == NORMAL


@pytest.mark.parametrize('field,value', [('count', True), ('count', 2001), ('last_frame_age_s', 2.0), ('last_frame_age_s', float('nan')), ('window_s', 100.0)])
def test_unavailable_or_stale_timing_is_not_reused(field, value):
  stats = timing()
  stats[field] = value
  assert valid_timing(stats) is None


def test_display_window_is_exact_bounded_and_read_only(monkeypatch):
  from jetlink.server import session

  monkeypatch.setattr(session.time, 'perf_counter', lambda: 30.0)
  frames = FrameStats(maxlen=2000)
  frames.samples.extend([(19.0, 999999, 999999), (20.0, 1000, 500), (29.0, 3000, 1500)])
  before = list(frames.samples)
  report = frames.display_summary()
  assert report['count'] == 2 and report['window_s'] == 10.0
  assert report['infer_ms'] == {'mean': 1.0, 'p99': 1.49, 'max': 1.5}
  assert report['server_ms']['max'] == 3.0
  assert frames.display_summary(since=29.0)['count'] == 1
  assert list(frames.samples) == before


def test_control_does_not_mix_new_engine_with_old_samples(monkeypatch):
  from jetlink.server import control

  now = [30.0]
  monkeypatch.setattr(control.time, 'perf_counter', lambda: now[0])
  frames = FrameStats()
  frames.samples.append((29.0, 1000, 500))
  server = object.__new__(ControlServer)
  server.host = NS(loaded=NS(sha256='a' * 64), frame_stats=frames)
  assert server._cmd_display_stats(None, {})['display_stats'] is None
  frames.samples.append((30.1, 3000, 1500))
  now[0] = 30.2
  assert server._cmd_display_stats(None, {})['display_stats']['count'] == 1
  server.host.loaded = NS(sha256='b' * 64)
  assert server._cmd_display_stats(None, {})['display_stats'] is None
  server.host.loaded = None
  assert server._cmd_display_stats(None, {})['display_stats'] is None


def test_sampler_accepts_real_top_level_control_reply_and_skips_events():
  a, b = socket.socketpair()
  sampler = object.__new__(RuntimeFooterSampler)
  sampler.sock = a
  sampler.sequence = 0
  sampler.pending = bytearray()
  try:
    b.sendall(
      json.dumps({'event': 'engine', 'state': 'ready'}).encode()
      + b'\n'
      + json.dumps({'event': 'reply', 'id': 1, 'ok': True, 'display_stats': timing()}).encode()
      + b'\n'
    )
    assert sampler._timing() == timing()
    request = json.loads(b.recv(4096))
    assert request == {'id': 1, 'cmd': 'display_stats'}
  finally:
    a.close()
    b.close()


def test_timing_readout_separates_host_window_from_latest_comma_sample():
  text = ''.join(text for text, _ in inference_segments({'timing': timing()}, comma_ms=48.9, include_comma=True))
  assert text == 'TRT 10s AVG:25.0 P99:28.0 MAX:31.0 ms | COMMA 48.9 ms | DROP 1s:--'
  text = ''.join(text for text, _ in inference_segments({}, include_comma=True))
  assert text == 'TRT 10s AVG:-- P99:-- MAX:-- ms | COMMA -- ms | DROP 1s:--'


@pytest.mark.parametrize('ms,color', [(0.1, NORMAL), (39.9, NORMAL), (40., NORMAL),
                                   (40.1, CAUTION), (43., CAUTION), (43.9, CAUTION), (44., HIGH), (90., HIGH)])
def test_comma_thresholds_match_the_visible_decimal_and_do_not_change_trt(ms, color):
  parts = inference_segments({'timing': timing()}, comma_ms=ms, comma_drops=3, include_comma=True)
  assert (f'COMMA {ms:.1f} ms', color) in parts
  assert ('DROP 1s:3', HIGH) in parts and parts[-2] == (' | ', (215, 220, 225))
  assert metric_color(35, 'infer') == CAUTION and metric_color(50, 'infer') == HIGH


@pytest.mark.parametrize('ms', [None, True, -1, 0, float('nan'), float('inf'), 10001])
def test_invalid_comma_timing_is_unknown_and_invalid_loss_never_looks_like_zero(ms):
  parts = inference_segments({}, comma_ms=ms, comma_drops=True, include_comma=True)
  assert ('COMMA -- ms', UNKNOWN) in parts and ('DROP 1s:--', HIGH) in parts


def diagnostic_snapshot(ms=44., **stats):
  from openpilot.cereal import log
  event = log.Event.new_message(valid=True, modelV2={'modelExecutionTime': ms / 1000,
                               'frameDropStats': {'valid': True, 'count': 3, 'windowEndMonoTime': 7_500_000_000, **stats}})
  record = {'sent': 8., 'received': {'modelV2': 7.9}, 'valid': {'modelV2': True},
            'alive': {'modelV2': True}, 'events': {'modelV2': base64.b64encode(event.to_bytes()).decode()}}
  return 100., record


def test_source_count_survives_hud_skips_and_float32_threshold_rounding():
  snapshot = diagnostic_snapshot()
  call, loss = model_diagnostics_sample(snapshot, 100.1)
  assert call == pytest.approx((44., 101.4)) and loss == (3, 102.)
  # Resampling cannot count the same publication as another lost frame.
  assert model_diagnostics_sample(snapshot, 100.2)[1] == loss
  assert ('COMMA 44.0 ms', HIGH) in inference_segments({}, comma_ms=call[0], include_comma=True)


@pytest.mark.parametrize('stats', [{'valid': False}, {'windowEndMonoTime': 0},
                                  {'windowEndMonoTime': 8_100_000_000}, {'windowEndMonoTime': 5_500_000_000}])
def test_unknown_reset_future_and_expired_source_window_are_not_zero(stats):
  call, loss = model_diagnostics_sample(diagnostic_snapshot(**stats), 100.1)
  assert call is not None and loss is None


def test_old_model_packet_is_compatible_and_missing_or_stale_input_clears_diagnostics():
  snapshot = diagnostic_snapshot()
  assert model_diagnostics_sample(snapshot, 100.5) == (None, None)
  assert model_diagnostics_sample(None, 100.) == (None, None)
  snapshot[1]['alive']['modelV2'] = False
  assert model_diagnostics_sample(snapshot, 100.1) == (None, None)


def test_legacy_packet_without_drop_metadata_displays_unknown_not_zero():
  from openpilot.cereal import log
  snapshot = diagnostic_snapshot()
  event = log.Event.new_message(valid=True, modelV2={'modelExecutionTime': .04})
  snapshot[1]['events']['modelV2'] = base64.b64encode(event.to_bytes()).decode()
  assert model_diagnostics_sample(snapshot, 100.1)[0] is not None
  assert model_diagnostics_sample(snapshot, 100.1)[1] is None


def test_renderer_decodes_once_per_second_and_clears_on_vehicle_gap(monkeypatch):
  import hud_health as hud
  import hud_protocol
  from cluster_live import standby_state

  clock, connected, calls = [100.], [True], []
  def snapshot():
    return clock[0], {'events': {'carState': 'present'}, 'alive': {'carState': connected[0]},
                     'valid': {'carState': connected[0]}, 'sent': clock[0]}
  monkeypatch.setattr(hud_protocol, 'read_snapshot', snapshot)
  monkeypatch.setattr(hud.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(hud, '_fresh', lambda *_: {})
  monkeypatch.setattr(hud.ClusterUiRenderer, 'render', lambda *_: None)
  monkeypatch.setattr(sys.modules['hud_runtime_stats'], 'model_diagnostics_sample',
                      lambda snapshot, now: calls.append(now) or ((40., now+1.5), (3, now+2.)))
  renderer = hud.HealthRenderer()
  for now in (100., 100.05, 100.5, 100.99, 101., 101.5):
    clock[0] = now
    renderer.render(standby_state())
  assert calls == [100., 101.] and renderer._model_drops[0] == 3
  connected[0], clock[0] = False, 101.6
  renderer.render(standby_state())
  assert renderer._model_call is renderer._model_drops is None


def test_comma_timing_uses_fresh_existing_model_event():
  from openpilot.cereal import log

  event = log.Event.new_message(valid=True, modelV2={'modelExecutionTime': .0415})
  record = {'sent': 8., 'received': {'modelV2': 7.9}, 'valid': {'modelV2': True},
            'alive': {'modelV2': True}, 'events': {'modelV2': base64.b64encode(event.to_bytes()).decode()}}
  assert model_call_sample((100., record), 100.1) == pytest.approx((41.5, 101.4))
  assert model_call_sample((100., record), 100.5) is None
  record['valid']['modelV2'] = False
  assert model_call_sample((100., record), 100.1) is None


def test_cached_host_timing_expires_before_other_resources(monkeypatch):
  import threading
  import hud_runtime_stats as stats

  sampler = object.__new__(RuntimeFooterSampler)
  sampler.lock = threading.Lock()
  sampler.value = {'updated': 100., 'timing': {**timing(), 'last_frame_age_s': .9}, 'gpu_percent': 20.}
  monkeypatch.setattr(stats.time, 'monotonic', lambda: 101.2)
  assert sampler.sample()['timing'] is None
  assert sampler.sample()['gpu_percent'] == 20.
