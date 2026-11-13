"""Owned HUD-child failures only; no devices, USB resets or vehicle restarts."""
import threading
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import hud_protocol as protocol
from hud_recovery import ExitedChild


class Child:
  def __init__(self, exited=False):
    self.code = 1 if exited else None
    self.stops = 0

  def poll(self):
    return self.code

  def terminate(self):
    self.stops += 1
    self.code = -15

  def kill(self):
    self.code = -9

  def wait(self, timeout=None):
    return self.code


def joined(slot):
  if slot.worker is not None:
    slot.worker.join(2.)
    assert not slot.worker.is_alive()


def test_exited_publisher_recovers_fresh_frames_without_recreating_session(tmp_path, monkeypatch):
  calls = []
  def start(command, **kwargs):
    calls.append(command)
    if len(calls) == 2:
      path = next(Path(item) for item in command if str(item).endswith('.packet'))
      path.write_bytes(protocol.HEADER.pack(10.) + b'new HUD frame')
    return Child(exited=len(calls) == 1)
  monkeypatch.setattr(protocol.subprocess, 'Popen', start)
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: 10.)
  publisher = protocol.Publisher(nav_map=True, rate_control=True)
  publisher.path = tmp_path / 'snapshot.packet'
  try:
    assert publisher.packet() is None
    joined(publisher.snapshot_child)
    assert publisher.packet() == b'new HUD frame'
    assert publisher.packet() is None  # No duplicate replay after recovery.
    assert len(calls) == 2 and '--nav-map' in calls[1] and '--rate-control' in calls[1]
  finally:
    publisher.close()
  assert not publisher.path.exists()


def test_stale_but_alive_child_is_never_restarted(tmp_path, monkeypatch):
  start = Mock(return_value=Child())
  monkeypatch.setattr(protocol.subprocess, 'Popen', start)
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: 100.)
  publisher = protocol.Publisher()
  publisher.path = tmp_path / 'packet'
  publisher.path.write_bytes(protocol.HEADER.pack(1.) + b'stale')
  for _ in range(100):
    assert publisher.packet() is None
  assert start.call_count == 1 and publisher.snapshot_child.worker is None
  publisher.close()


def test_backoff_is_bounded_and_spawn_failure_does_not_escape():
  now, count = [100.], [0]
  def fail():
    count[0] += 1
    raise OSError('PC synthetic spawn failure')
  slot = ExitedChild(fail, clock=lambda: now[0])
  assert slot.last_failure == 'OSError' and count[0] == 1
  assert slot.request_if_exited() is False
  for _ in range(12):
    now[0] = slot.retry_at
    assert slot.request_if_exited()
    joined(slot)
    assert slot.delay <= 30.
    assert slot.request_if_exited() is False
  slot.close()


def test_live_child_attached_between_poll_and_lock_is_not_replaced():
  start = Mock(return_value=Child(exited=True))
  slot = ExitedChild(start)
  alive = Child()
  old = slot.process
  def raced_poll():
    slot.process = alive
    return 1
  old.poll = raced_poll
  try:
    assert not slot.request_if_exited()
    assert start.call_count == 1 and slot.process is alive
  finally:
    slot.close()


def test_thread_creation_failure_is_nonfatal_and_obeys_backoff(monkeypatch):
  now = [100.]
  slot = ExitedChild(lambda: Child(exited=True), clock=lambda: now[0])
  thread = Mock()
  thread.start.side_effect = RuntimeError('synthetic resource pressure')
  monkeypatch.setattr(threading, 'Thread', Mock(return_value=thread))
  assert slot.request_if_exited() is False
  assert slot.request_if_exited() is False and thread.start.call_count == 1
  assert slot.worker is None and slot.last_failure == 'RuntimeError'
  slot.close()


def test_restart_spawn_never_waits_on_the_snapshot_caller():
  entered, release, returned = threading.Event(), threading.Event(), threading.Event()
  children = []
  def start():
    if children:
      entered.set()
      assert release.wait(5.)
    child = Child(exited=not children)
    children.append(child)
    return child
  slot = ExitedChild(start)
  caller = threading.Thread(target=lambda: (slot.request_if_exited(), returned.set()))
  try:
    caller.start()
    assert entered.wait(1.)
    assert returned.wait(1.) and not release.is_set()
    for _ in range(100):
      assert slot.request_if_exited() is False
    assert len(children) == 1
  finally:
    release.set()
    caller.join(2.)
    joined(slot)
    slot.close()


def test_close_during_spawn_retires_late_owned_child():
  entered, release = threading.Event(), threading.Event()
  children = []
  def start():
    if children:
      entered.set()
      assert release.wait(2.)
    child = Child(exited=not children)
    children.append(child)
    return child
  cleanup = Mock()
  slot = ExitedChild(start, cleanup)
  assert slot.request_if_exited() and entered.wait(1.)
  closing = threading.Thread(target=slot.close)
  closing.start()
  assert slot.closed.wait(1.)
  release.set()
  closing.join(2.)
  assert not closing.is_alive() and len(children) == 2
  assert children[1].poll() is not None and children[1].stops == 1
  assert not slot.request_if_exited()
  assert cleanup.called


def test_packet_paths_do_not_collide_across_sessions(monkeypatch):
  monkeypatch.setattr(protocol.subprocess, 'Popen', lambda *a, **k: Child())
  first, second = protocol.Publisher(), protocol.Publisher()
  try:
    assert first.path != second.path
  finally:
    first.close(); second.close()


def test_recovery_keeps_stale_data_invalid_until_new_frame(tmp_path, monkeypatch):
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: 10.)
  monkeypatch.setattr(protocol.subprocess, 'Popen', lambda *a, **k: Child(exited=True))
  publisher = protocol.Publisher()
  publisher.path = tmp_path / 'packet'
  publisher.path.write_bytes(protocol.HEADER.pack(1.) + b'old')
  try:
    assert publisher.packet() is None
    joined(publisher.snapshot_child)
    assert publisher.packet() is None
    assert not publisher.path.exists()
  finally:
    publisher.close()


def test_actual_owned_process_exit_reconnects_to_new_frames(tmp_path, monkeypatch):
  popen = subprocess.Popen
  count = [0]
  def start(command, **kwargs):
    count[0] += 1
    if count[0] == 1:
      return popen([sys.executable, '-c', 'raise SystemExit(1)'])
    path = next(str(item) for item in command if str(item).endswith('.packet'))
    script = '''
import pathlib,struct,sys,time
path=pathlib.Path(sys.argv[1])
for _ in range(200):
  temporary=path.with_suffix('.tmp')
  temporary.write_bytes(struct.pack('<d',time.monotonic())+b'live')
  temporary.replace(path)
  time.sleep(.05)
'''
    return popen([sys.executable, '-c', script, path])
  monkeypatch.setattr(protocol.subprocess, 'Popen', start)
  publisher = protocol.Publisher()
  publisher.path = tmp_path / 'real.packet'
  original_pid = publisher.process.pid
  try:
    publisher.process.wait(timeout=2.)
    end, received = time.monotonic() + 3., None
    while time.monotonic() < end and received is None:
      received = publisher.packet()
      time.sleep(.01)
    assert received == b'live' and count[0] == 2
    assert publisher.process.pid != original_pid
  finally:
    publisher.close()
  assert publisher.process.poll() is not None
