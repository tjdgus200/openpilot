"""Recover only an exited, owned display child without restarting a model link.

No monitor thread or timer runs while healthy. The existing failed snapshot read
requests at most one temporary low-priority restart worker, with 2..30s backoff.
An alive child is never killed because data is stale or inference is busy.
"""
import logging
import os
import subprocess
import threading
import time

LOG = logging.getLogger(__name__)


def stop_child(process):
  if process is None:
    return
  try:
    if process.poll() is None:
      process.terminate()
      try:
        process.wait(timeout=1.)
      except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.)
  except (OSError, subprocess.TimeoutExpired):
    pass  # Only this Popen-owned child is addressed, never another process.


def lower_priority():
  try:
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
  except (AttributeError, OSError):
    pass


class ExitedChild:
  def __init__(self, start, cleanup=lambda: None, clock=time.monotonic):
    self.start, self.cleanup, self.clock = start, cleanup, clock
    self.lock = threading.Lock()
    self.closed = threading.Event()
    self.worker = None
    self.process = None
    self.retry_at, self.delay = 0., 2.
    self.healthy_since = None
    self.last_failure = ''
    try:
      self.process = start()
    except Exception as error:
      self.last_failure = type(error).__name__
      self.retry_at = clock() + self.delay

  def healthy(self):
    now = self.clock()
    with self.lock:
      if self.healthy_since is None:
        self.healthy_since = now
      elif now - self.healthy_since >= 2.:
        self.delay = 2.
        self.last_failure = ''

  def request_if_exited(self):
    if self.closed.is_set():
      return False
    process = self.process
    if process is not None and process.poll() is None:
      return False
    with self.lock:
      now = self.clock()
      if (self.closed.is_set() or now < self.retry_at
          or self.worker is not None and self.worker.is_alive()):
        return False
      # A previous recovery may have attached a live child after the first poll.
      if self.process is not None and self.process.poll() is None:
        return False
      self.retry_at = now + self.delay
      self.delay = min(30., self.delay * 2)
      self.healthy_since = None
      self.worker = threading.Thread(target=self._restart, name='hud-publisher-recover', daemon=True)
      try:
        self.worker.start()
      except (OSError, RuntimeError) as error:
        self.last_failure = type(error).__name__
        self.worker = None
        return False
      return True

  def _cleanup(self):
    try:
      self.cleanup()
    except OSError:
      pass

  def _restart(self):
    lower_priority()
    child = None
    try:
      if self.closed.is_set():
        return
      LOG.warning('Owned HUD snapshot publisher exited; attempting display-only recovery')
      self._cleanup()
      child = self.start()
      with self.lock:
        if not self.closed.is_set():
          self.process = child
          child = None  # Ownership passes to close(); no model/session actions.
    except Exception as error:
      self.last_failure = type(error).__name__
      LOG.warning('HUD snapshot recovery unavailable (%s); bounded retry retained', self.last_failure)
    finally:
      if child is not None:
        stop_child(child)
      if self.closed.is_set():
        self._cleanup()

  def close(self):
    self.closed.set()
    with self.lock:
      process, worker = self.process, self.worker
    stop_child(process)
    if worker is not None:
      worker.join(timeout=3.)
    # A late spawn sees closed, retires its own child and repeats cleanup.
    # Each Publisher has a unique packet path, so it cannot overwrite a new session.
    self._cleanup()
