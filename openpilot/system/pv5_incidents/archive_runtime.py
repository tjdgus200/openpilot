"""Use tombstoned's existing observer, never a model/control owner or incoming tunnel."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from openpilot.system.pv5_incidents.archive_gate import Admission, evaluate


class Coordinator:
  def __init__(self, config, path, log_root, commit, clock=time.monotonic):
    self.config, self.path, self.log_root, self.commit, self.clock = config, path, log_root, commit, clock
    self.admission, self.child = Admission(), None
    self.next_try, self.retry, self.idle, self.was_allowed = 0.0, 2.0, False, False
    self.last_reason = ''
    self.child_start = None
    self.segment_seen = None
    expires = config.get('qualification_expires_at')
    self.qualification_deadline = None if expires is None else clock() + max(0, expires - time.time())

  @staticmethod
  def start_time(pid):
    return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]

  def cancel(self):
    child, self.child = self.child, None
    started, self.child_start = self.child_start, None
    if child is not None:
      child.stdin.close()  # The worker also self-terminates if its permission pipe closes.
      # Do not reap first: the reserved leader PID prevents a group-ID reuse
      # race, and an orphaned nmcli is still killed if the leader just exited.
      try:
        if started is not None and self.start_time(child.pid) == started and os.getpgid(child.pid) == child.pid:
          os.killpg(child.pid, signal.SIGKILL)
      except (FileNotFoundError, ProcessLookupError):
        pass
      try:
        child.wait(timeout=0.1)
      except subprocess.TimeoutExpired:
        pass
      child.stdout.close()

  def tick(self, sm, now):
    # A staged rollout is not permanent qualification. Bound it with monotonic
    # time as well as wall time, so clock adjustments cannot extend the trial.
    expires = self.config.get('qualification_expires_at')
    if expires is not None and (time.time() >= expires or now >= self.qualification_deadline):
      self.cancel()
      self.last_reason = 'qualification_expired'
      return
    # Existing encoder segment-change messages wake a stationary dashcam scan;
    # no timer, directory watcher or repeated immutable-segment rescan at idle.
    if (
      sm['deviceState'].started
      and sm.seen.get('roadEncodeIdx', False)
      and sm.valid.get('roadEncodeIdx', False)
      and 0 <= now - sm.logMonoTime['roadEncodeIdx'] / 1e9 < 1
    ):
      segment = sm['roadEncodeIdx'].segmentNum
      if segment != self.segment_seen:
        self.segment_seen, self.idle = segment, False
    decision = evaluate(sm, now, self.config['qualified_modes'], self.config['vehicle'] == 'KIA_PV5')
    allowed = self.admission.tick(decision, now)
    if not allowed:
      self.cancel()
      if self.was_allowed:
        self.idle = False
      self.was_allowed = False
      self.last_reason = decision.reason
      return
    self.was_allowed = True
    exited = self.child is not None and os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    if exited:
      result = {}
      try:
        raw = os.read(self.child.stdout.fileno(), 4097)
        if len(raw) <= 4096:
          result = json.loads(raw)
      except (ValueError, OSError):
        pass
      self.last_reason = result.get('reason', 'worker_budget')
      self.cancel()
      self.idle = self.last_reason == 'idle'
      pause = 60.0 if self.last_reason in ('storage', 'network_profile', 'worker_error') else self.retry
      self.next_try = now + pause
      self.retry = 2.0 if self.last_reason in ('idle', 'budget') else min(300.0, self.retry * 2)
    if self.idle or now < self.next_try:
      return  # No filesystem/network polling when a completed detached session is idle.
    if self.child is None:
      self.child = subprocess.Popen(
        [
          '/usr/bin/nice',
          '-n',
          '19',
          '/usr/bin/ionice',
          '-c',
          '3',
          sys.executable,
          '-m',
          'openpilot.system.pv5_incidents.archive_worker',
          '--config',
          str(self.path),
          '--log-root',
          str(self.log_root),
          '--catalog',
          '/data/pv5-incidents/archive.sqlite',
          '--commit',
          self.commit,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
      )
      os.set_blocking(self.child.stdin.fileno(), False)
      os.set_blocking(self.child.stdout.fileno(), False)
      self.child_start = self.start_time(self.child.pid)
    try:
      os.write(self.child.stdin.fileno(), f'{now:.9f}\n'.encode())
    except (OSError, BlockingIOError):
      self.cancel()
      self.next_try = now + 2.0


def requested():
  # No imports/subscribers/worker when unconfigured, unqualified or disabled.
  try:
    if not Path('/data/pv5-pc-archive.json').is_file():
      return None
    from openpilot.system.pv5_incidents.archive_worker import CONFIG, configuration

    return configuration(CONFIG), CONFIG
  except (ImportError, OSError, ValueError, TypeError, KeyError):
    return None


def create(request, commit):
  from openpilot.system.hardware.hw import Paths

  config, path = request
  if not Path('/usr/bin/ionice').is_file() or not Path('/usr/bin/nice').is_file():
    return None
  return Coordinator(config, path, Paths.log_root(), commit)
