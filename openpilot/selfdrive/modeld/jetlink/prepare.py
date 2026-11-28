"""Prepare the optional camera adapter without sharing modeld's tinygrad state."""
from concurrent.futures import Future, InvalidStateError
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

PREPARE_TIMEOUT = 60


class WarpPreparation:
  def __init__(self, width, height, *, use_cache=False, refresh_cache=False):
    self.future = Future()
    self.cache_status = 'uncached' if use_cache else 'disabled'
    self.cancelled = threading.Event()
    self.thread = threading.Thread(target=self._prepare, args=(width, height, use_cache, refresh_cache), name='jetlink-prepare', daemon=True)
    self.thread.start()

  def _prepare(self, width, height, use_cache, refresh_cache):
    try:
      # Do not inherit modeld's realtime policy/core for the supervisor or compiler.
      if hasattr(os, 'sched_setscheduler'):
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        os.sched_setaffinity(0, {0, 1, 2})
      started = time.monotonic()
      with tempfile.TemporaryDirectory(prefix='jetlink-warp-', dir='/dev/shm' if sys.platform == 'linux' else None) as directory:
        target = Path(directory) / 'warp.pkl'
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(Path(p or '.').resolve()) for p in sys.path))
        # This adapter always targets the local QCOM GPU, regardless of any
        # unrelated default-device discovery or optional external model backend.
        env.update(DEV='QCOM', WARP_DEV='QCOM')
        process = None
        try:
          with (Path(directory) / 'prepare.log').open('w+b') as output:
            args = [sys.executable, '-m', __name__, str(width), str(height), str(os.getpid()), str(target)]
            if use_cache:
              args.append('refresh' if refresh_cache else 'cache')
            process = subprocess.Popen(args, cwd=directory, env=env, stdout=output, stderr=subprocess.STDOUT)
            while process.poll() is None:
              if self.cancelled.wait(.05):
                raise InterruptedError('Jetlink adapter preparation cancelled')
              if time.monotonic() - started > PREPARE_TIMEOUT:
                raise TimeoutError('Jetlink adapter preparation timed out')
            if process.returncode:
              output.seek(0)
              raise RuntimeError('Jetlink adapter preparation failed: ' + output.read().decode(errors='replace')[-2000:])
            if use_cache:
              try:
                with target.with_suffix('.status').open('rb') as status:
                  result = status.read(16)
                if result in (b'hit', b'rebuilt'):
                  self.cache_status = result.decode('ascii')
              except OSError:
                pass
            self.future.set_result((target.read_bytes(), time.monotonic() - started))
        finally:
          if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    except InvalidStateError:
      pass  # Disconnected after preparation finished, before delivery.
    except Exception as exc:
      try:
        self.future.set_exception(exc)
      except InvalidStateError:
        pass
  def close(self):
    self.cancelled.set()
    self.future.cancel()


def main():
  import ctypes
  import signal
  width, height, parent = map(int, sys.argv[1:4])
  # A manager/modeld restart must not leave an orphan GPU compiler running.
  if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
    raise RuntimeError('Jetlink preparation parent unavailable')
  os.nice(19)
  target = Path(sys.argv[4])
  if sys.argv[5:] in (['cache'], ['refresh']):
    from openpilot.selfdrive.modeld.jetlink import warp_cache
    result = warp_cache.prepare(width, height, target, refresh=sys.argv[5] == 'refresh')
    try:
      target.with_suffix('.status').write_text(result)
    except OSError:
      pass
  else:
    from openpilot.selfdrive.modeld.jetlink.model import Warp
    Warp(width, height).save_prepared(target, width, height)


if __name__ == '__main__':
  main()
