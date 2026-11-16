"""Optional preview lifecycle tests; owned PC children only, no live camera/model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import hud_camera as camera
import hud_protocol as protocol


class Child:
  def __init__(self, exited=False):
    self.code = 7 if exited else None
    self.stops = 0

  def poll(self): return self.code
  def terminate(self):
    self.stops += 1
    self.code = -15
  def kill(self): self.code = -9
  def wait(self, timeout=None): return self.code


@pytest.fixture
def owned_paths(tmp_path, monkeypatch):
  real_path = Path
  def path(value):
    value = str(value)
    return tmp_path / real_path(value).name if value.startswith('/dev/shm/carrot-jetlink-cameras-') else real_path(value)
  monkeypatch.setattr(camera, 'Path', path)
  return tmp_path


def fake_clock(monkeypatch, now=100.):
  clock = [now]
  monkeypatch.setattr(camera, 'time', NS(monotonic=lambda: clock[0]))
  monkeypatch.setattr(protocol, 'time', NS(monotonic=lambda: clock[0]))
  return clock


def builder(monkeypatch, clock):
  from openpilot.common import jetlink_status as status
  monkeypatch.setattr(status, 'read_status', lambda: ({}, {}))
  monkeypatch.setattr(status, '_fresh', lambda *a: {})
  monkeypatch.setattr(status, 'badge', lambda *a: None)
  obj = object.__new__(protocol.SnapshotBuilder)
  settings = {'ClusterHud': 1, 'IsOnroad': True, 'ClusterHudDebug': 0}
  obj.params = NS(get_int=lambda k: settings[k], get_bool=lambda k: settings[k])
  obj.camera = None
  class SM:
    updated = dict.fromkeys(protocol.SERVICES, False)
    valid = alive = {'carState': True}
    recv_time = {'carState': clock[0]}
    logMonoTime = {'carState': int(clock[0] * 1e9)}
    def update(self, _): self.recv_time['carState'] = clock[0]
    def __getitem__(self, _): return NS(gearShifter='drive', vEgo=10.)
  obj.sm = SM()
  obj.next_send, obj.next_params = 0., 1e9
  obj.cached = {'existingService': 'unchanged'}
  obj.settings = {}
  obj.host_release = obj.nav_map = None
  obj.target_fps, obj.requested_fps, obj.rate_control = 10, 10, True
  obj.network = NS(address=lambda: '')
  obj.battery_snapshot = lambda _: None
  return obj, settings


def test_repeated_spawn_failure_is_bounded_and_other_snapshot_fields_survive(owned_paths, monkeypatch):
  clock = fake_clock(monkeypatch)
  fail = Mock(side_effect=OSError('owned synthetic spawn failure'))
  monkeypatch.setattr(camera.subprocess, 'Popen', fail)
  obj, _ = builder(monkeypatch, clock)
  for _ in range(200):
    packet = json.loads(obj.packet())
    assert packet['cameras'] == {} and packet['events'] == {'existingService': 'unchanged'}
    clock[0] = obj.next_send
  assert fail.call_count == 4
  assert obj.camera.retry_delay == 30.
  assert obj.camera.close()


def test_optional_constructor_failure_does_not_expire_other_snapshot_fields(monkeypatch):
  clock = fake_clock(monkeypatch)
  fail = Mock(side_effect=RuntimeError('optional import/setup failure'))
  monkeypatch.setattr(camera, 'CameraPublisher', fail)
  obj, _ = builder(monkeypatch, clock)
  for _ in range(200):
    packet = json.loads(obj.packet())
    assert packet['cameras'] == {} and packet['events']['existingService'] == 'unchanged'
    clock[0] = obj.next_send
  assert 9 <= fail.call_count <= 10 and obj.camera is None


def test_optional_latest_failure_is_rate_limited_and_nonfatal(monkeypatch):
  clock = fake_clock(monkeypatch)
  reads = []
  class Failing:
    @property
    def latest(self):
      reads.append(clock[0])
      raise RuntimeError('optional read failure')
  obj, _ = builder(monkeypatch, clock)
  obj.camera = Failing()
  for _ in range(200):
    assert json.loads(obj.packet())['events']['existingService'] == 'unchanged'
    clock[0] = obj.next_send
  assert 9 <= len(reads) <= 10


@pytest.mark.parametrize('value', ['bad', [], {'updated': 'bad', 'cameras': {}},
  {'updated': 99.699, 'cameras': {'road': 1}}, {'updated': 101., 'cameras': {'road': 1}},
  {'updated': 100., 'cameras': []}, {'updated': float('nan'), 'cameras': {}}])
def test_invalid_stale_or_future_preview_is_rejected_without_restarting_live_child(owned_paths, monkeypatch, value):
  fake_clock(monkeypatch)
  start = Mock(return_value=Child())
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  obj = camera.CameraPublisher()
  try:
    obj.path.write_text(json.dumps(value))
    for _ in range(200): assert obj.latest == {}
    assert start.call_count == 1 and obj.process.stops == 0
  finally:
    assert obj.close()


def test_backoff_resets_only_after_sustained_fresh_frames(owned_paths, monkeypatch):
  clock = fake_clock(monkeypatch)
  start = Mock(side_effect=[OSError('spawn'), Child()])
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  obj = camera.CameraPublisher()
  clock[0] += 2
  assert obj.latest == {} and start.call_count == 2
  assert obj.retry_delay == 4.
  try:
    for i in range(22):
      obj.path.write_text(json.dumps({'updated': clock[0], 'cameras': {'road': i}}))
      assert obj.latest == {'road': i}
      clock[0] += .1
    assert obj.retry_delay == 2.
  finally:
    assert obj.close()


def test_no_reuse_of_dead_child_preview_even_when_file_is_fresh(owned_paths, monkeypatch):
  clock = fake_clock(monkeypatch)
  start = Mock(side_effect=[Child(True), Child()])
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  obj = camera.CameraPublisher()
  obj.path.write_text(json.dumps({'updated': clock[0], 'cameras': {'road': 'dead'}}))
  assert obj.latest == {} and not obj.path.exists() and obj.process is None
  clock[0] += 1.99
  assert obj.latest == {} and start.call_count == 1
  clock[0] += .01
  assert obj.latest == {} and start.call_count == 2
  assert obj.close()


def test_failed_close_keeps_owned_slot_until_exit_confirmed(owned_paths, monkeypatch):
  clock = fake_clock(monkeypatch)
  child = Child()
  child.terminate = Mock(side_effect=OSError('synthetic temporary close failure'))
  start = Mock(return_value=child)
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  obj, settings = builder(monkeypatch, clock)
  assert obj.camera_previews() == {}
  settings['IsOnroad'] = False
  assert obj.camera_previews() == {} and obj.camera is not None
  settings['IsOnroad'] = True
  for _ in range(200): assert obj.camera_previews() == {}
  assert start.call_count == 1 and child.terminate.call_count == 1
  child.terminate = lambda: setattr(child, 'code', -15)
  clock[0] += 2.
  settings['IsOnroad'] = False
  assert obj.camera_previews() == {} and obj.camera is None


def test_disabled_and_offroad_do_not_start_preview(monkeypatch):
  clock = fake_clock(monkeypatch)
  start = Mock()
  monkeypatch.setattr(camera, 'CameraPublisher', start)
  obj, settings = builder(monkeypatch, clock)
  settings['ClusterHud'] = 0
  for _ in range(200): assert obj.camera_previews() == {}
  settings.update(ClusterHud=1, IsOnroad=False)
  for _ in range(200): assert obj.camera_previews() == {}
  start.assert_not_called()


def test_actual_owned_exit_recovers_then_disable_reaps_child(owned_paths, monkeypatch):
  popen = subprocess.Popen
  children = []
  script = "import json,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); [(p.write_text(json.dumps({'updated':time.monotonic(),'cameras':{'road':'fresh'}})),time.sleep(.03)) for _ in range(200)]"
  def start(command, **kwargs):
    assert all(p.poll() is not None for p in children)
    args = [sys.executable, '-c', 'raise SystemExit(7)'] if not children else [sys.executable, '-c', script, command[-1]]
    child = popen(args, **kwargs)
    children.append(child)
    return child
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  clock = [time.monotonic()]
  obj, settings = builder(monkeypatch, clock)
  try:
    assert obj.camera_previews() == {}
    children[0].wait(timeout=3.)
    assert obj.camera_previews() == {}  # Detect exit and start backoff.
    end = time.monotonic() + 4.
    result = {}
    while time.monotonic() < end and not result:
      result = obj.camera_previews()
      time.sleep(.01)
    assert result == {'road': 'fresh'} and len(children) == 2
    settings['IsOnroad'] = False
    assert obj.camera_previews() == {} and obj.camera is None
    assert all(p.poll() is not None for p in children)
  finally:
    if obj.camera is not None: obj.camera.close()
    for child in children:
      if child.poll() is None:
        child.kill(); child.wait(timeout=3.)


def test_actual_owned_repeated_onoff_has_no_fd_or_child_leak(owned_paths, monkeypatch):
  popen = subprocess.Popen
  children = []
  def start(command, **kwargs):
    assert all(p.poll() is not None for p in children)
    child = popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
    children.append(child)
    return child
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  clock = [time.monotonic()]
  obj, settings = builder(monkeypatch, clock)
  before = len(os.listdir('/proc/self/fd'))
  try:
    for _ in range(30):
      settings['IsOnroad'] = True
      obj.camera_previews()
      settings['IsOnroad'] = False
      assert obj.camera_previews() == {} and obj.camera is None
    assert len(children) == 30 and all(p.poll() is not None for p in children)
    assert len(os.listdir('/proc/self/fd')) <= before
    assert not list(owned_paths.iterdir())
  finally:
    for child in children:
      if child.poll() is None:
        child.kill(); child.wait(timeout=3.)


def test_actual_owned_sigterm_ignoring_child_is_reaped(owned_paths, monkeypatch):
  popen = subprocess.Popen
  ready = owned_paths / 'ready'
  def start(command, **kwargs):
    script = 'import signal,pathlib,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(30)'
    return popen([sys.executable, '-c', script, str(ready)], **kwargs)
  monkeypatch.setattr(camera.subprocess, 'Popen', start)
  obj = camera.CameraPublisher()
  child = obj.process
  try:
    end = time.monotonic() + 3.
    while not ready.exists() and time.monotonic() < end: time.sleep(.01)
    assert ready.exists()
    start_time = time.monotonic()
    assert obj.close()
    assert child.poll() == -9 and time.monotonic() - start_time < 2.5
    assert obj.close()
  finally:
    if child.poll() is None:
      child.kill(); child.wait(timeout=3.)

def test_preview_loop_reuses_two_clients_and_preserves_onroad_scheduler(owned_paths, monkeypatch):
  import openpilot.common.display_scheduling as scheduling
  import openpilot.common.params as params
  clock = [100.]
  rounds = [0]
  clients, updates = [], []
  class StopLoop(Exception): pass
  class Client:
    def __init__(self, name, stream, conflate):
      assert name == 'camerad' and conflate
      self.connected = False
      self.connects = self.receives = 0
      self.frame_id = 1
      self.timestamp_eof = 100_000_000_000
      clients.append(self)
    def is_connected(self): return self.connected
    def connect(self, blocking):
      assert blocking is False
      self.connected = True
      self.connects += 1
      return True
    def recv(self, timeout_ms):
      assert timeout_ms == 0
      self.receives += 1
      if self.receives == 50: self.connected = False
      return NS(width=1928, height=1208)
  monkeypatch.setitem(sys.modules, 'msgq.visionipc', NS(VisionIpcClient=Client,
    VisionStreamType=NS(VISION_STREAM_ROAD=0, VISION_STREAM_WIDE_ROAD=2)))
  monkeypatch.setattr(scheduling, 'DisplayScheduler', lambda priority, enabled: NS(update=updates.append))
  monkeypatch.setattr(params, 'Params', lambda: NS(get_bool=lambda key: rounds[0] < 100))
  monkeypatch.setattr(camera.ctypes, 'CDLL', lambda _: NS(prctl=lambda *a: None))
  monkeypatch.setattr(camera, 'os', NS(getppid=lambda: 2, replace=os.replace))
  monkeypatch.setattr(camera, 'jpeg_preview', lambda _: b'jpeg')
  monkeypatch.setattr(camera, 'preview_nv12', lambda _: b'preview')
  def sleep(_):
    rounds[0] += 1
    clock[0] += .1
    if rounds[0] == 200: raise StopLoop
  monkeypatch.setattr(camera, 'time', NS(monotonic=lambda: clock[0], sleep=sleep))
  with pytest.raises(StopLoop): camera.CameraPublisher.run(owned_paths / 'frame.json')
  assert len(clients) == 2 and all(c.connects == 2 and c.receives == 200 for c in clients)
  assert updates == [True] * 100 + [False] * 100
  assert set(json.loads((owned_paths / 'frame.json').read_text())['cameras']) == {'road', 'wide'}