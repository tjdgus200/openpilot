"""Bounded fault injection only: no USB gadget, manager, or vehicle is touched."""
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from jetlink.transport import ffs
from jetlink.transport.base import LinkTimeout, StreamTransport
from openpilot.selfdrive.modeld.jetlink import daemon
from openpilot.selfdrive.modeld.jetlink.cleanup import CleanupFailure


class Worker:
  def __init__(self, alive=False):
    self.alive = alive

  def is_alive(self):
    return self.alive


def owner_at(path, worker=None):
  owner = daemon.CarrotTransport.__new__(daemon.CarrotTransport)
  StreamTransport.__init__(owner)
  owner.gadget, owner.bound_udc = str(path), 'controller'
  owner._reader = worker
  owner._write_guard = SimpleNamespace(thread=Worker())
  owner._close_threads = []
  owner._closing = False
  for name in ('_state_fd', 'ep_in', 'ep_out', 'ep0'):
    setattr(owner, name, -1)
  return owner


def finish_base_close(owner):
  owner._closing, owner._reader = True, None


@pytest.mark.parametrize('kind', ['reader', 'watchdog', 'closer'])
def test_late_worker_must_finish_before_read_only_recovery(tmp_path, monkeypatch, kind):
  (tmp_path / 'UDC').write_text('')
  worker = Worker(True)
  owner = owner_at(tmp_path, worker if kind == 'reader' else None)
  if kind == 'watchdog':
    owner._write_guard.thread = worker
  if kind == 'closer':
    owner._close_threads = [worker]
  closes = []
  def close(value):
    closes.append(value)
    finish_base_close(value)
  monkeypatch.setattr(ffs.FfsTransport, 'close', close)
  failure = daemon.close_resources(owner)
  assert isinstance(failure, CleanupFailure)
  for _ in range(100):
    assert not failure.cleared()
  assert len(closes) == 1  # Recovery polls never redo native close/unbind.
  worker.alive = False
  assert failure.cleared()
  assert len(closes) == 1


@pytest.mark.parametrize('remaining', ['bound', 'missing_udc', 'fd', 'aio', 'writer'])
def test_dead_threads_are_insufficient_with_remaining_ownership(tmp_path, monkeypatch, remaining):
  (tmp_path / 'UDC').write_text('')
  owner = owner_at(tmp_path)
  owner._closing = owner._cleanup_started = True
  if remaining == 'bound':
    (tmp_path / 'UDC').write_text('controller')
  elif remaining == 'missing_udc':
    (tmp_path / 'UDC').unlink()
  elif remaining == 'fd':
    owner.ep0 = 42  # Only a sentinel; never closed by this observation test.
  elif remaining == 'aio':
    owner._aio_reader = SimpleNamespace(context=object())
  else:
    owner._request_writer = SimpleNamespace(context=object())
  assert not owner.cleanup_complete()


def test_all_failed_proofs_survive_successful_and_duplicate_closes(tmp_path, monkeypatch):
  (tmp_path / 'UDC').write_text('')
  worker = Worker(True)
  owner = owner_at(tmp_path, worker)
  monkeypatch.setattr(ffs.FfsTransport, 'close', finish_base_close)
  first = daemon.close_resources(owner)
  second = daemon.close_resources(owner)
  pending = CleanupFailure.combine(second, first)
  assert not pending.cleared()
  worker.alive = False
  assert pending.cleared()
  good = SimpleNamespace(close=lambda: None)
  assert CleanupFailure.combine(daemon.close_resources(good), pending).cleared()
  bad = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError('unknown ownership')))
  assert not CleanupFailure.combine(daemon.close_resources(bad), pending).cleared()
  assert not CleanupFailure.combine(pending, 'unclassified earlier fault').cleared()
  assert not CleanupFailure('unknown', []).cleared()
  assert not CleanupFailure('probe fails', [lambda: (_ for _ in ()).throw(OSError())]).cleared()


def test_endpoint_close_worker_is_tracked_until_it_has_returned(tmp_path, monkeypatch):
  (tmp_path / 'UDC').write_text('')
  owner = owner_at(tmp_path)
  owner._closing = owner._cleanup_started = True
  owner.ep_out = 42
  entered, release = threading.Event(), threading.Event()
  def close(fd):
    assert fd == 42
    entered.set()
    assert release.wait(2)
  monkeypatch.setattr(ffs, '_close_quietly', close)
  try:
    owner._close_fds(('ep_out',), Worker(True))
    assert entered.wait(1)
    assert owner.ep_out == -1 and len(owner._close_threads) == 1
    assert not owner.cleanup_complete()
  finally:
    release.set()
    for thread in owner._close_threads:
      thread.join(1)
  assert owner.cleanup_complete()


def test_failed_closer_start_preserves_the_descriptor(tmp_path, monkeypatch):
  owner = owner_at(tmp_path)
  owner.ep_out = 42
  def fail_start(self):
    raise RuntimeError('cannot start worker')
  monkeypatch.setattr(threading.Thread, 'start', fail_start)
  with pytest.raises(RuntimeError, match='cannot start'):
    owner._close_fds(('ep_out',), Worker(True))
  assert owner.ep_out == 42 and not owner._close_threads


def test_proof_checks_are_five_seconds_apart_without_stopping_status_updates():
  calls = []
  pending = CleanupFailure('wait', [lambda: calls.append(True) or len(calls) >= 2])
  assert not pending.poll(100.)
  for i in range(1, 5):
    assert not pending.poll(100. + i)
  assert len(calls) == 1
  assert pending.poll(105.) and len(calls) == 2


@pytest.mark.parametrize('mode', ['usb', 'auto'])
@pytest.mark.parametrize('fault', ['reader', 'closer', 'constructor', 'persistent', 'mixed', 'power'])
def test_daemon_rechecks_without_rebinding_live_resources(tmp_path, monkeypatch, mode, fault):
  owners, reports, waits, events = [], [], [], []
  worker = Worker(True)
  original = daemon.CarrotTransport
  gadget = tmp_path / 'gadget'
  gadget.mkdir()
  (gadget / 'UDC').write_text('')
  udcs = tmp_path / 'udc'
  (udcs / 'controller').mkdir(parents=True)
  (udcs / 'controller' / 'current_speed').write_text('super-speed')
  def path(value):
    return udcs if value == '/sys/class/udc' else tmp_path / 'ipc' if value == daemon.SOCKET else Path(value)
  monkeypatch.setattr(daemon, 'Path', path)
  monkeypatch.setattr(daemon, 'open', lambda *a: (tmp_path / 'lock').open('w'), raising=False)
  monkeypatch.setattr(daemon.fcntl, 'flock', lambda *a: None)
  monkeypatch.setattr(daemon, 'transport_mode', lambda: mode)
  monkeypatch.setenv('JETLINK_PROTOCOL', 'auto')
  monkeypatch.setattr(daemon, 'update_affinity', lambda: None)
  monkeypatch.setattr(daemon, 'host_attached', lambda: True)
  monkeypatch.setattr(daemon, 'gc', SimpleNamespace(disable=lambda: None, freeze=lambda: None, collect=lambda: None))
  monkeypatch.setattr(daemon.os, 'sched_setscheduler', lambda *a: None)
  monkeypatch.setattr(daemon.os, 'sched_setaffinity', lambda *a: None)
  monkeypatch.setattr(daemon.os, 'chmod', lambda *a: None)
  listener = SimpleNamespace(bind=lambda *a: None, listen=lambda *a: None, settimeout=lambda *a: None, close=lambda: None)
  monkeypatch.setattr(daemon.socket, 'socket', lambda *a: listener)
  power = SimpleNamespace(step=lambda: None, protected=True, error='test power policy failure')
  monkeypatch.setattr(daemon, 'UsbInputPower', lambda: power)
  monkeypatch.setattr(daemon, 'publish', lambda state, **kw: reports.append((state, kw)))
  monkeypatch.setattr(daemon, 'UsbCompletionPriority', lambda: SimpleNamespace(close=lambda: None))
  def setup(value):
    assert not owners or all(owner.cleanup_complete() for owner in owners)
    events.append('setup')
  monkeypatch.setattr(daemon, 'setup_gadget', setup)
  monkeypatch.setattr(ffs.FfsTransport, 'close', finish_base_close)
  def make_owner(*a, **kw):
    # The global factory is replaced, but each object uses the real safety checks.
    owner = original.__new__(original)
    StreamTransport.__init__(owner)
    owner.gadget, owner.bound_udc = str(gadget), 'controller'
    owner._reader = worker if not owners and fault != 'closer' else None
    owner._write_guard = SimpleNamespace(thread=Worker())
    owner._close_threads = [worker] if not owners and fault == 'closer' else []
    owner._closing = False
    owners.append(owner)
    if fault == 'constructor' and len(owners) == 1:
      owner.close()
    return owner
  monkeypatch.setattr(daemon, 'CarrotTransport', make_owner)
  class Cable:
    listener = object()
    def __init__(self, owner):
      self.owner = owner
    def start(self):
      pass
    def close(self):
      pass
  monkeypatch.setattr(daemon, 'MobileCable', Cable)
  class Auto:
    diagnostic = 'test arbitration'
    def __init__(self, owner, cable, **kw):
      self.owner = owner
    def accept(self, timeout):
      return self.owner, 'usb'
  monkeypatch.setattr(daemon, 'AutoTransport', Auto)
  class Client:
    last_state = None
    def __init__(self, owner, **kw):
      self.t = owner
    def hello(self, **kw):
      events.append('hello')
      if len(owners) == 1:
        raise LinkTimeout('injected original disconnect')
      assert owners[0].cleanup_complete()
      raise KeyboardInterrupt
    def close(self):
      if fault == 'mixed':
        raise RuntimeError('unknown other resource failure')
      self.t.close()
  monkeypatch.setattr(daemon, 'JetlinkClient', Client)
  retry_cycles, clock = [], [0.]
  def sleep(delay):
    clock[0] += delay
    if delay == 2:
      retry_cycles.append(delay)
      assert len(retry_cycles) < 6, (events, reports[-2:])
      return
    if fault == 'power' and not power.protected:
      assert len(owners) == 1  # Power protection is checked before any new owner.
      raise KeyboardInterrupt
    assert reports[-1][0] == 'blocked' and delay == 1
    waits.append(delay)
    assert events.count('setup') == 1
    if len(waits) == 3 and fault != 'persistent':
      worker.alive = False
    if fault == 'power' and len(waits) == 3:
      power.protected = False
    if len(waits) == 12:
      assert fault in ('persistent', 'mixed')
      raise KeyboardInterrupt
  monkeypatch.setattr(daemon, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
  with pytest.raises(KeyboardInterrupt):
    daemon.main()
  if fault in ('persistent', 'mixed', 'power'):
    assert len(owners) == 1
  else:
    assert len(owners) == 2 and waits == [1] * 5
    assert any(info.get('preparation', {}).get('stage') == 'cleanup-complete' for _, info in reports)
  if fault == 'persistent':
    assert len(waits) == 12  # No attempt cap or new threads during a persistent fault.
