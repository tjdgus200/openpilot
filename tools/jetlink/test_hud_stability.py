"""Replay telemetry gaps and sustained idle admission, without physical devices."""
import ast
import base64
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from hud_rate import IdleBudget, PARAM, RATES, selection


@pytest.mark.parametrize('fps', RATES)
def test_actual_host_loop_keeps_panel_open_through_short_and_long_telemetry_gaps(monkeypatch, fps):
  import hud
  import hud_quality as quality
  source = Path(hud.__file__)
  tree = ast.parse(source.read_text(encoding='utf-8'))
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  ready = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'dashboard_ready')
  watch = next(n for n in main.body if isinstance(n, ast.ClassDef) and n.name == 'QualityWatch')
  loop = next(n for n in reversed(main.body) if isinstance(n, ast.While))
  now, boot_polls, gap = [100.], [0], [False]
  encoded = {PARAM: base64.b64encode(str(RATES.index(fps)).encode()).decode(), 'IsOnroad': 'MQ=='}
  record = {'params': encoded, 'alive': {'carState': True}, 'valid': {'carState': True},
            'events': {'carState': 'valid event'}}
  def snapshot():
    return None if boot_polls[0] < 2 or gap[0] else (now[0], record)
  monkeypatch.setattr(hud, 'read_snapshot', snapshot)
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now[0])
  settings = hud.DisplayParams()
  events = []
  class Owner:
    def __enter__(self):
      assert boot_polls[0] == 2
      events.append('owner acquired')
      return True
    def __exit__(self, *args):
      events.append('owner released')
  def sleep(seconds):
    now[0] += seconds
    boot_polls[0] += 1
  def render(**kwargs):
    events.append('display opened')
    for age in (.1, .6, 2.1, 10., .2):
      gap[0] = True
      now[0] += age
      assert not env['dashboard_ready']()
      assert env['QualityWatch']().read() == 0
      assert settings.get_bool('IsOnroad')  # Missing telemetry is not ignition OFF.
      gap[0] = False
      now[0] += .1
      assert env['dashboard_ready']()
      assert env['QualityWatch']().read() == 0
    events.append('display ended')  # Deliberate end of the PC fixture.
  env = {'selection': selection, 'PARAM': PARAM, 'override': None, 'requested': 0,
         'settings': settings, 'hud_quality': NS(), 'hud_runtime_stats': NS(),
         'sys': NS(argv=[]), 'base': ['hud.py'], 'codec_options': quality.codec_options,
         'NAMES': quality.NAMES, 'RATES': RATES, 'extra_args': [], 'cluster': NS(main=render),
         'panel_owner': lambda **kwargs: Owner(), 'CpuJpegError': quality.CpuJpegError,
         'read_snapshot': snapshot, 'time': NS(sleep=sleep)}
  runner = ast.FunctionDef(name='run', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
      kw_defaults=[], defaults=[]), body=[ast.Global(names=['requested']), loop], decorator_list=[])
  exec(compile(ast.fix_missing_locations(ast.Module(body=[ready, watch, runner], type_ignores=[])),
               str(source), 'exec'), env)
  env['run']()
  assert events == ['owner acquired', 'display opened', 'display ended', 'owner released']
  assert env['sys'].argv[-1] == str(fps)
  # A real saved FPS change still requests exactly the existing HUD-only reopen.
  encoded[PARAM] = base64.b64encode(str((RATES.index(fps) + 1) % len(RATES)).encode()).decode()
  now[0] += 1.
  assert env['QualityWatch']().read() == 1
  # Explicit fresh ignition OFF remains visible to the original output gate.
  encoded['IsOnroad'] = 'MA=='
  now[0] += 1.
  assert not settings.get_bool('IsOnroad')


@pytest.mark.parametrize('write_s', [.003, .006, .008])
def test_sixty_second_twenty_hz_replay_does_not_create_two_second_telemetry_blackouts(monkeypatch, write_s):
  if sys.platform == 'win32':
    monkeypatch.setitem(sys.modules, 'fcntl', NS())
  from openpilot.selfdrive.modeld.jetlink import daemon
  now, frame, last = [100.], [0], [-1]
  sends = []
  monkeypatch.setattr(daemon.time, 'monotonic', lambda: now[0])
  monkeypatch.setattr(daemon, 'request_ready', lambda *args: False)
  def packet():
    if last[0] == frame[0]:
      return None
    last[0] = frame[0]
    return b'fresh snapshot'
  def send(kind, seq, parts):
    start = 100. + frame[0] * .05
    assert now[0] >= start + .03  # No HUD writes during model execution.
    assert start + .05 - now[0] > .004 + budget.send_budget
    sends.append(now[0])
    now[0] += write_s
  client = NS(t=NS(send=send), _next_seq=lambda: len(sends) + 1)
  publisher = NS(packet=packet)
  budget = IdleBudget()
  for number in range(1200):
    frame[0] = number
    start = 100. + number * .05
    budget.model_started(start)
    now[0] = start + .03
    budget.completed(now[0], .03)
    while now[0] < start + .05:
      daemon.publish_idle_hud(client, publisher, budget, object())
      now[0] += .002
  assert len(sends) > 300
  assert max(b - a for a, b in zip(sends, sends[1:])) < .5
  assert now[0] - sends[-1] < .5


@pytest.mark.parametrize('inference,read_s,write_s', [(.025,.003,.002), (.030,.003,.003), (.035,.008,.002)])
@pytest.mark.parametrize('fps', RATES)
def test_sixty_second_fresh_snapshots_make_progress_with_spare_inference_headroom(monkeypatch, fps, inference, read_s, write_s):
  from openpilot.selfdrive.modeld.jetlink import daemon
  now, last_sent = [100.], [-1.]
  sent = []
  monkeypatch.setattr(daemon.time, 'monotonic', lambda: now[0])
  monkeypatch.setattr(daemon, 'request_ready', lambda *a: False)
  budget = IdleBudget()
  def packet():
    stamp = int((now[0] - 100.) * fps + 1e-7)
    if stamp == last_sent[0]:
      return None
    now[0] += read_s
    last_sent[0] = stamp
    return b'latest snapshot'
  def send(kind, seq, parts):
    assert kind == daemon.HUD_MESSAGE
    assert now[0] + write_s < budget.last_model + budget.period - .004
    sent.append(now[0])
    now[0] += write_s
  client = NS(t=NS(send=send), _next_seq=lambda: len(sent))
  publisher = NS(packet=packet, target_fps=fps)
  for frame in range(1200):
    start = 100. + frame * .05
    budget.model_started(start)
    now[0] = start + inference
    budget.completed(now[0], inference)
    while now[0] < start + .05:
      daemon.publish_idle_hud(client, publisher, budget, object())
      now[0] += .001
  assert len(sent) >= 500
  assert max(b-a for a,b in zip(sent, sent[1:])) < .3
  assert now[0] - sent[-1] < .3


def test_expensive_navigation_does_not_poison_the_hud_snapshot_budget():
  budget = IdleBudget()
  budget.model_started(10.)
  budget.observe(10.021, {'display_media_send_ms': 20.})
  assert budget.allowed(10.025)
  assert not budget.allowed(10.025, media=True)
  assert budget.send_budget == .002 and budget.write_budget == .002


def test_remote_vehicle_data_still_expires_during_a_gap(monkeypatch):
  import hud
  from openpilot.cereal import log
  event = log.Event.new_message(deviceState={'started': True})
  encoded = base64.b64encode(event.to_bytes()).decode()
  snapshot = [ (100., {'sent': 100., 'received': {'deviceState': 100.},
      'mono': {'deviceState': 100_000_000_000}, 'valid': {'deviceState': True},
      'alive': {'deviceState': True}, 'events': {'deviceState': encoded}}) ]
  monkeypatch.setattr(hud, 'read_snapshot', lambda: snapshot[0])
  remote = hud.RemoteSubMaster(['deviceState'])
  remote.update()
  assert remote.alive['deviceState'] and remote.valid['deviceState']
  snapshot[0] = None
  remote.update()
  assert not remote.alive['deviceState'] and not remote.valid['deviceState']
  assert remote.steering_warning == 0
