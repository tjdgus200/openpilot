"""Nonblocking means every supervisor tick, including the forced-kill/restart path."""
import importlib.util
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('process_stop_probe', Path(__file__).with_name('process_stop_probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def lifecycle():
  clock = SimpleNamespace(now=100.0)
  clock.monotonic = lambda: clock.now
  clock.sleep = lambda seconds: setattr(clock, 'now', clock.now + seconds)
  ns = probe.load_process(clock=clock)
  p = ns['PythonProcess']('owned', 'unused', lambda *_: True)
  child = SimpleNamespace(exitcode=None, pid=123, is_alive=lambda: child.exitcode is None)
  child.join = lambda: (_ for _ in ()).throw(AssertionError('nonblocking supervisor called join'))
  p.proc = child
  signals = []
  p.signal = signals.append
  return ns, p, child, clock, signals


def test_repeated_stop_never_waits_and_preserves_grace(lifecycle):
  ns, p, child, clock, signals = lifecycle
  p.stop(block=False)
  assert signals == [signal.SIGINT]
  for offset in (0, 0.1, 1, 4.999):
    clock.now = 100 + offset
    p.stop(block=False)
    assert clock.now == 100 + offset
    assert signals == [signal.SIGINT]
  clock.now = 105
  for _ in range(200):
    p.stop(block=False)
  assert signals == [signal.SIGINT, signal.SIGKILL]
  assert p.proc is child and p.shutting_down
  assert p.get_process_state_msg().running and not p.get_process_state_msg().shouldBeRunning
  child.exitcode = -signal.SIGKILL
  assert p.stop(block=False) == -signal.SIGKILL
  assert p.proc is None and not p.shutting_down and not ns['reports']


@pytest.mark.parametrize('retry', [True, False])
def test_nonblocking_retry_option(lifecycle, retry):
  ns, p, child, clock, signals = lifecycle
  p.stop(block=False)
  clock.now += 6
  p.stop(block=False, retry=retry)
  assert signals == ([signal.SIGINT, signal.SIGKILL] if retry else [signal.SIGINT])
  assert clock.now == 106


@pytest.mark.parametrize('sigkill', [True, False])
def test_default_initial_signal(lifecycle, sigkill):
  ns, p, child, clock, signals = lifecycle
  p.sigkill = sigkill
  p.stop(block=False)
  clock.now += 6
  p.stop(block=False)
  assert signals == ([signal.SIGKILL] if sigkill else [signal.SIGINT, signal.SIGKILL])


def test_explicit_initial_kill_is_not_repeated(lifecycle):
  ns, p, child, clock, signals = lifecycle
  p.stop(block=False, sig=signal.SIGKILL)
  clock.now += 6
  p.stop(block=False)
  assert signals == [signal.SIGKILL]


@pytest.mark.parametrize('kind', ['NativeProcess', 'PythonProcess'])
def test_start_during_pending_stop_does_not_wait_or_duplicate(lifecycle, kind):
  ns, old, child, clock, signals = lifecycle
  p = ns[kind]('owned', '.', ['unused'], lambda *_: True) if kind == 'NativeProcess' else old
  p.proc = child
  p.signal = signals.append
  p.stop(block=False)
  for _ in range(200):
    p.start()
  assert clock.now == 100
  assert p.proc is child
  assert signals == [signal.SIGINT]


def test_repeated_supervision_keeps_process_state_truthful(lifecycle):
  ns, p, child, clock, signals = lifecycle
  for _ in range(20):
    ns['ensure_running']([p], True, not_run=['owned'])
  assert clock.now == 100 and len(signals) == 1
  assert p.proc is child and p.shutting_down
  ns['ensure_running']([p], True)
  assert p.proc is child and p.shutting_down
  assert clock.now == 100


@pytest.mark.parametrize('exitcode', [0, 7, -6])
def test_already_exited_child_is_reaped_and_crash_reported_once(lifecycle, exitcode):
  ns, p, child, clock, signals = lifecycle
  child.exitcode = exitcode
  assert p.stop(block=False) == exitcode
  p.stop(block=False)
  assert ns['reports'] == ([] if exitcode == 0 else [('owned', 123, exitcode)])
  assert p.proc is None and not signals


def test_blocking_cleanup_waits_and_escalates_as_before(lifecycle):
  ns, p, child, clock, signals = lifecycle
  child.join = lambda: setattr(child, 'exitcode', -signal.SIGKILL)
  assert p.stop() == -signal.SIGKILL
  assert clock.now >= 105
  assert signals == [signal.SIGINT, signal.SIGKILL]
  assert p.proc is None


def test_blocking_cleanup_without_retry_preserves_live_child(lifecycle):
  ns, p, child, clock, signals = lifecycle
  assert p.stop(retry=False) is None
  assert clock.now >= 105 and p.proc is child and p.shutting_down
  assert signals == [signal.SIGINT]


def test_grace_is_reset_for_a_new_child(lifecycle):
  ns, p, child, clock, signals = lifecycle
  p.stop(block=False)
  child.exitcode = 0
  p.stop(block=False)
  clock.now = 200
  child.exitcode = None
  p.proc = child
  p.stop(block=False)
  clock.now = 204
  p.stop(block=False)
  assert signals == [signal.SIGINT, signal.SIGINT]


def test_real_uncooperative_children_do_not_stall_supervisor(tmp_path):
  import json
  import os
  import subprocess
  import sys
  if os.name != 'posix':
    pytest.skip('owned SIGINT/SIGKILL lifecycle requires POSIX')
  result = subprocess.run([sys.executable, str(Path(__file__).with_name('process_stop_probe.py'))],
                          capture_output=True, text=True, timeout=20)
  assert result.returncode == 0, result.stdout + result.stderr
  data = json.loads(result.stdout)
  assert data['max_supervision_s'] < 0.5
  assert 5 <= data['elapsed_s'] < 10
  assert data['reaped'] == data['owned_children'] == 2
  assert data['unexpected_crash_reports'] == 0


@pytest.mark.parametrize('wanted', [True, False])
def test_manager_reports_pending_required_restart_as_invalid(wanted):
  import ast
  import os
  import time
  from unittest.mock import Mock
  path = Path(__file__).parents[1] / 'manager.py'
  node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'manager_thread')
  p = SimpleNamespace(proc=SimpleNamespace(is_alive=lambda: True), name='owned', shutting_down=False)
  p.get_process_state_msg = lambda: SimpleNamespace(name='owned', running=True, pid=123,
                                                    shouldBeRunning=not p.shutting_down)
  messages = []
  class End(Exception):
    pass
  class SubMaster:
    def __init__(self, *_args, **_kwargs):
      self.step = 0
    def update(self, _timeout):
      if self.step == 3:
        raise End
      p.shutting_down = self.step == 1
      self.step += 1
    def __getitem__(self, key):
      return [] if key == 'pandaStates' else SimpleNamespace(started=False)
    def all_checks(self, _topics):
      return False  # Keep the production power-watchdog file outside this test.
  params = SimpleNamespace(get=lambda *_: None, get_bool=lambda *_: False)
  messaging = SimpleNamespace(SubMaster=SubMaster, PubMaster=lambda *_: SimpleNamespace(send=lambda _topic, msg: messages.append(msg)),
                              new_message=lambda _topic, valid: SimpleNamespace(valid=valid, managerState=SimpleNamespace()))
  ns = {'UpdateStatus': object, 'cloudlog': Mock(), 'Params': lambda: params, 'os': os, 'time': time,
        'UNREGISTERED_DONGLE_ID': 'fake', 'messaging': messaging,
        'XiaogeStartupGate': lambda: SimpleNamespace(update=lambda *_: False),
        'write_onroad_params': lambda *_: None, 'managed_processes': {'owned': p},
        'ensure_running': lambda *_args, **_kwargs: [p] if wanted else []}
  exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
  with pytest.raises(End):
    ns['manager_thread'](SimpleNamespace(update=lambda *_: False))
  assert [m.valid for m in messages] == ([True, False, True] if wanted else [True, True, True])
  assert all(m.managerState.processes[0].pid == 123 and m.managerState.processes[0].running for m in messages)
