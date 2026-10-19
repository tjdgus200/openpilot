import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'tools/jetlink'), str(ROOT/'third_party/jetlink'),
               str(ROOT/'openpilot/selfdrive/carrot/cluster')]
from hud_runtime_stats import (gpu_percent, footer_lines, valid_timing, RuntimeFooterSampler,
                               metric_color, NORMAL, CAUTION, HIGH, UNKNOWN)
from jetlink.server.session import FrameStats
from jetlink.server.control import ControlServer


def timing():
  return {'window_s': 10.0, 'count': 200, 'last_frame_age_s': .1,
          'infer_ms': {'mean': 25., 'p99': 28., 'max': 31.},
          'server_ms': {'mean': 27., 'p99': 30., 'max': 33.}}


@pytest.mark.parametrize('raw,expected', [('0', 0.), ('1000', 100.), ('537', 53.7),
                                          ('-1', None), ('1001', None), ('nan', None), ('bad', None)])
def test_gpu_load_units_and_invalid_values(tmp_path, raw, expected):
  path = tmp_path/'load'; path.write_text(raw)
  assert gpu_percent((tmp_path/'missing', path)) == expected


def test_footer_separates_hosts_and_names_timing_scope():
  lines = footer_lines(NS(cpu_used_percent=12., memory_used_percent=49.),
                       {'cpu_percent': 8., 'memory_percent': 27., 'gpu_percent': 53.7, 'timing': timing()})
  assert len(lines) == 2
  assert 'COMMA CPU:12% MEM:49%' in lines[0]
  assert 'ORIN CPU:8% MEM:27% GPU:54%' in lines[0]
  assert '10s' in lines[1] and 'AVG:25.0' in lines[1] and 'P99:28.0' in lines[1] and 'MAX:31.0' in lines[1]


def test_resource_colors_are_display_only_and_gpu_load_is_not_a_fault():
  assert metric_color(None,'cpu') == UNKNOWN
  assert metric_color(79,'cpu') == NORMAL and metric_color(80,'cpu') == CAUTION
  assert metric_color(95,'cpu') == HIGH
  assert metric_color(85,'memory') == CAUTION and metric_color(95,'memory') == HIGH
  assert metric_color(35,'infer') == CAUTION and metric_color(50,'infer') == HIGH
  assert metric_color(100,'gpu') == NORMAL


@pytest.mark.parametrize('field,value', [('count', True), ('count', 2001), ('last_frame_age_s', 2.),
                                         ('last_frame_age_s', float('nan')), ('window_s', 100.)])
def test_unavailable_or_stale_timing_is_not_reused(field, value):
  stats = timing(); stats[field] = value
  assert valid_timing(stats) is None


def test_display_window_is_exact_bounded_and_read_only(monkeypatch):
  from jetlink.server import session
  monkeypatch.setattr(session.time, 'perf_counter', lambda: 30.)
  frames = FrameStats(maxlen=2000)
  frames.samples.extend([(19.,999999,999999),(20.,1000,500),(29.,3000,1500)])
  before = list(frames.samples)
  report = frames.display_summary()
  assert report['count'] == 2 and report['window_s'] == 10.
  assert report['infer_ms'] == {'mean': 1., 'p99': 1.49, 'max': 1.5}
  assert report['server_ms']['max'] == 3.
  assert frames.display_summary(since=29.)['count'] == 1
  assert list(frames.samples) == before


def test_control_does_not_mix_new_engine_with_old_samples(monkeypatch):
  from jetlink.server import control
  now = [30.]
  monkeypatch.setattr(control.time, 'perf_counter', lambda: now[0])
  frames = FrameStats();frames.samples.append((29.,1000,500))
  server = object.__new__(ControlServer)
  server.host = NS(loaded=NS(sha256='a'*64),frame_stats=frames)
  assert server._cmd_display_stats(None,{})['display_stats'] is None
  frames.samples.append((30.1,3000,1500));now[0]=30.2
  assert server._cmd_display_stats(None,{})['display_stats']['count'] == 1
  server.host.loaded=NS(sha256='b'*64)
  assert server._cmd_display_stats(None,{})['display_stats'] is None
  server.host.loaded=None
  assert server._cmd_display_stats(None,{})['display_stats'] is None


def test_sampler_accepts_real_top_level_control_reply_and_skips_events():
  a,b=socket.socketpair()
  sampler=object.__new__(RuntimeFooterSampler)
  sampler.sock=a;sampler.sequence=0;sampler.pending=bytearray()
  try:
    b.sendall(json.dumps({'event':'engine','state':'ready'}).encode()+b'\n'+
              json.dumps({'event':'reply','id':1,'ok':True,'display_stats':timing()}).encode()+b'\n')
    assert sampler._timing() == timing()
    request=json.loads(b.recv(4096))
    assert request == {'id':1,'cmd':'display_stats'}
  finally:a.close();b.close()
