"""Real private child-process teardown; no DHCP, sockets, configfs or vehicle."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).with_name('setup_mobile.sh')


@pytest.fixture
def child(tmp_path):
  compiler = shutil.which('cc')
  if compiler is None:
    pytest.skip('native private-child fixture needs a C compiler')
  program = tmp_path / 'dnsmasq'
  subprocess.run([compiler, '-x', 'c', '-', '-o', str(program)], input='''
    #include <signal.h>
    #include <unistd.h>
    int main(void) {
      sigset_t mask; sigemptyset(&mask); sigaddset(&mask, SIGTERM);
      sigprocmask(SIG_BLOCK, &mask, 0);
      for (;;) pause();
    }
  ''', text=True, check=True, capture_output=True)
  state = tmp_path / 'state'; state.mkdir()
  leases = tmp_path / 'leases'; leases.mkdir()
  args = [str(program), f'--pid-file={state / "dnsmasq.pid"}', '--port=0', '--conf-file=/dev/null',
          f'--dhcp-leasefile={leases / "dnsmasq.leases"}']
  process = subprocess.Popen(args)
  try:
    proc = Path('/proc') / str(process.pid)
    deadline = time.monotonic() + 3
    while not int((proc / 'status').read_text().split('SigBlk:\t', 1)[1].splitlines()[0], 16) & (1 << (signal.SIGTERM - 1)):
      assert time.monotonic() < deadline
      time.sleep(.01)
    start = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()[19]
    (state / 'dnsmasq.pid').write_text(str(process.pid))
    (state / 'dnsmasq.owner').write_text(json.dumps({'pid': process.pid, 'start': start}))
    yield process, proc, state, leases
  finally:
    if process.poll() is None:
      process.kill()
    process.wait(timeout=3)


def stop_helper(state, leases, legacy=False, prefix=''):
  source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
  if legacy:
    source = "import os; os.__dict__.pop('pidfd_open', None)\n" + source
  source = prefix + '\n' + source
  return subprocess.run([sys.executable, '-c', source, str(state), 'stop', str(leases)],
                        capture_output=True, text=True, timeout=8)


@pytest.mark.parametrize('legacy', [False, True])
def test_term_timeout_kills_only_recorded_private_child_and_confirms_exit(child, legacy):
  process, proc, state, leases = child
  result = stop_helper(state, leases, legacy)
  assert result.returncode == 0, result.stderr
  assert process.wait(timeout=3) == -signal.SIGKILL
  assert not (state / 'dnsmasq.pid').exists() and not (state / 'dnsmasq.owner').exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_changed_process_identity_is_never_signalled_or_forgotten(child, legacy):
  process, proc, state, leases = child
  (state / 'dnsmasq.owner').write_text(json.dumps({'pid': process.pid, 'start': '0'}))
  result = stop_helper(state, leases, legacy)
  assert result.returncode != 0 and 'PID was reused' in result.stderr
  assert process.poll() is None
  assert (state / 'dnsmasq.pid').exists() and (state / 'dnsmasq.owner').exists()


def test_previously_exited_owned_zombie_releases_records_without_an_executable(child):
  process, proc, state, leases = child
  os.kill(process.pid, signal.SIGKILL)  # Own test child only; leave it unreaped.
  deadline = time.monotonic() + 3
  while (proc / 'stat').read_text().rsplit(') ', 1)[1].split()[0] != 'Z':
    assert time.monotonic() < deadline
    time.sleep(.01)
  result = stop_helper(state, leases)
  assert result.returncode == 0, result.stderr
  assert not (state / 'dnsmasq.pid').exists() and not (state / 'dnsmasq.owner').exists()


def test_unkillable_process_keeps_ownership_and_refuses_rebind(child):
  process, proc, state, leases = child
  prefix = '''
import os, time, itertools
os.kill = lambda *a: None  # Simulated stuck kernel task; no signal is sent.
ticks = itertools.count()
time.monotonic = lambda: next(ticks)
time.sleep = lambda _: None
'''
  result = stop_helper(state, leases, legacy=True, prefix=prefix)
  assert result.returncode != 0 and 'refusing rebind' in result.stderr
  assert process.poll() is None
  assert (state / 'dnsmasq.pid').exists() and (state / 'dnsmasq.owner').exists()


def test_legacy_identity_is_checked_again_before_force_stop(child):
  process, proc, state, leases = child
  prefix = '''
import os, time, itertools
from pathlib import Path
signals = []
os.kill = lambda pid, sig: signals.append(sig)
original = Path.read_bytes
Path.read_bytes = lambda p: b'changed\\0' if p.name == 'cmdline' and signals else original(p)
ticks = itertools.count()
time.monotonic = lambda: next(ticks)
time.sleep = lambda _: None
'''
  result = stop_helper(state, leases, legacy=True, prefix=prefix)
  assert result.returncode != 0 and 'PID changed before signaling' in result.stderr
  assert process.poll() is None
  assert (state / 'dnsmasq.pid').exists() and (state / 'dnsmasq.owner').exists()
