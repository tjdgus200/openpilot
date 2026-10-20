"""One USB panel owner; an eligible HUD can request diagnostic-screen handoff."""
from contextlib import contextmanager
import os
import json
from pathlib import Path
import time

# logind RemoveIPC can unlink /dev/shm locks when the final SSH/probe session
# ends, even while system services still hold them. RuntimeDirectory survives.
DIRECTORY = Path('/run/carrot-jetlink-display')
if not DIRECTORY.is_dir():  # Legacy images without the boot display service.
  DIRECTORY = Path('/tmp') / ('carrot-jetlink-display-' + str(getattr(os, 'getuid', lambda: 0)()))


def process_identity(pid):
  # A reused PID from a dead HUD must not suppress diagnostics indefinitely.
  value = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
  return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(), value[19]]


def hud_requested(directory=DIRECTORY):
  try:
    request = json.loads((directory / 'hud-request').read_text())
    pid = int(request['pid'])
    if pid <= 1:
      return False
    return request['identity'] == process_identity(pid)
  except (OSError, ValueError, KeyError, TypeError, IndexError):
    return False


@contextmanager
def panel_owner(hud=False, directory=DIRECTORY, ready=None):
  import fcntl
  directory.mkdir(mode=0o700, parents=True, exist_ok=True)
  request = directory / 'hud-request'
  lock = (directory / 'owner.lock').open('a')
  acquired = False
  requested = False
  def release_request():
    try:
      value = json.loads(request.read_text())
      if isinstance(value, dict) and value.get('pid') == os.getpid() and value.get('identity') == process_identity(os.getpid()):
        request.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
      pass
  try:
    if hud:
      if ready is not None and not ready():
        yield False
        return
      temporary = request.with_suffix('.new')
      temporary.write_text(json.dumps({'pid': os.getpid(), 'identity': process_identity(os.getpid())}))
      temporary.replace(request)
      requested = True
    while True:
      try:
        if hud and ready is not None and not ready():
          break
        if not hud and hud_requested(directory):
          break
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        break
      except BlockingIOError:
        if not hud:
          break
        time.sleep(.1)
    if requested:
      release_request()
    yield acquired
  finally:
    if requested:
      release_request()
    if acquired:
      fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
