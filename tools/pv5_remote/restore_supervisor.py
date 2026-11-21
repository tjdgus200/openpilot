#!/usr/bin/env python3
"""Boot-only recovery of the already-qualified guard; never admits networking."""
import errno
import hashlib
import json
import os
import re
import runpy
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path('/data/pv5-remote-access')
UNIT = 'pv5-conditional-remote.service'
ATTEMPT_TIMEOUT = 25
PERMANENT = frozenset({
  'matching_code_qualification_required', 'home_resource_policy_required',
  'qualified_runtime_interpreter_changed', 'unexpected_trial_dropin',
  'persistent_unit_requires_manual_migration', 'foreign_unit_not_replaced',
  'active_unit_identity', 'existing_session_active_nothing_stopped',
  'owner_comma_local_install_required', 'systemd_required', 'unsafe_install_lock',
  'qualification_size', 'enrolled_binary_identity', 'existing_openssh_required',
  'qualified_interpreter_metadata_invalid',
})
STAGES = frozenset({'entry', 'boot_qualification', 'runtime_unit_restore'})


def protected(path, directory=False):
  st = path.lstat()
  if path.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022 or (not path.is_dir() if directory else not path.is_file()):
    raise ValueError('unsafe_boot_metadata')


def configured():
  protected(ROOT, directory=True)
  path = ROOT / 'conditional_modes.json'
  protected(path)
  if path.stat().st_size > 4096:
    raise ValueError('qualification_size')
  cfg = json.loads(path.read_bytes())
  if cfg.get('boot_restore') is not True or cfg.get('trial') is not False:
    return False
  home = cfg.get('qualified_modes') == ['detached_home'] and cfg.get('resource_policy') == 'detached_home_uncapped_v1'
  park = cfg.get('qualified_modes') == ['parked_vehicle'] and cfg.get('resource_policy') == 'parked_vehicle_capped_v1'
  if not (home or park):
    raise ValueError('home_resource_policy_required')
  script = ROOT / 'conditional_remote.py'
  protected(script)
  if script.stat().st_size > 131072:
    raise ValueError('qualification_size')
  if cfg.get('guard_sha256') != hashlib.sha256(script.read_bytes().replace(b'\r\n', b'\n')).hexdigest():
    raise ValueError('matching_code_qualification_required')
  return True


def record(state, attempt, reason='', stage='', elapsed=0):
  # A single replaced small file, not an ever-growing log or an AI event source.
  path = ROOT / 'boot_restore_status.json'
  if path.exists() or path.is_symlink():
    protected(path)
  data = json.dumps({'state': state, 'attempt': attempt, 'reason': reason, 'stage': stage,
                     'elapsed_seconds': round(elapsed, 3), 'updated_at': int(time.time())}, separators=(',', ':')).encode()
  fd, name = tempfile.mkstemp(prefix='.boot-restore-', dir=ROOT)
  temp = Path(name)
  try:
    with os.fdopen(fd, 'wb') as handle:
      os.fchmod(handle.fileno(), 0o600)
      handle.write(data)
    os.replace(temp, path)
  finally:
    temp.unlink(missing_ok=True)


def run_bounded(args, timeout):
  child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           start_new_session=True)
  try:
    output, _ = child.communicate(timeout=timeout)
  except BaseException:
    # Kill only this attempt's process group, never an existing guard or its jobs.
    try:
      os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    child.communicate()
    raise
  if len(output) > 16384:
    raise ValueError('restore_output_budget')
  return child.returncode, output


def qualified_interpreter():
  # Select only the interpreter in the already-enrolled protected unit. Do not
  # use sudo's PATH, rewrite the unit, migrate qualification, or restart a guard.
  if not configured():
    raise ValueError('qualified_interpreter_metadata_invalid')
  unit = ROOT / UNIT
  protected(unit)
  if unit.stat().st_size > 16384:
    raise ValueError('qualified_interpreter_metadata_invalid')
  data = unit.read_bytes()
  pattern = rb'^ExecStart=(/[A-Za-z0-9_./-]{1,255}) ' + re.escape(str(ROOT / 'conditional_remote.py').encode()) + rb' --guard$'
  matches = re.findall(pattern, data, re.MULTILINE)
  if len(matches) != 1:
    raise ValueError('qualified_interpreter_metadata_invalid')
  interpreter = matches[0].decode('ascii')
  if '..' in Path(interpreter).parts:
    raise ValueError('qualified_interpreter_metadata_invalid')
  module = runpy.run_path(str(ROOT / 'conditional_remote.py'))
  cfg = json.loads((ROOT / 'conditional_modes.json').read_bytes())
  expected = (module['unit_text'](interpreter, cfg['resource_policy']) if cfg['resource_policy'] == 'parked_vehicle_capped_v1' else module['unit_text'](interpreter))
  if data != expected.encode():
    raise ValueError('qualified_interpreter_metadata_invalid')
  executable = Path(interpreter).resolve(strict=True)
  protected(executable)
  if not os.access(executable, os.X_OK):
    raise ValueError('qualified_interpreter_metadata_invalid')
  return interpreter


def attempt_restore():
  try:
    interpreter = qualified_interpreter()
    code, output = run_bounded([interpreter, '-B', str(ROOT / 'conditional_remote.py'), '--restore-runtime'], ATTEMPT_TIMEOUT)
  except subprocess.TimeoutExpired:
    return 'retry', 'attempt_timeout', 'entry'
  except ValueError as error:
    return 'blocked', str(error) if str(error) in PERMANENT else 'qualified_interpreter_metadata_invalid', 'boot_qualification'
  except OSError:
    return 'retry', 'restore_execution_failed', 'entry'
  try:
    result = json.loads(output)
    if not isinstance(result, dict):
      raise ValueError()
  except (ValueError, TypeError):
    return 'retry', 'restore_result_unavailable', 'entry'
  stage = result.get('stage') if result.get('stage') in STAGES else 'entry'
  reason = result.get('reason')
  if reason in PERMANENT:
    return 'blocked', reason, stage
  if result.get('reason') == 'not_enabled':
    return 'disabled', 'not_enabled', stage
  if code or not (result.get('boot_restore_requested') is True or result.get('already_running') is True):
    reason = 'install_lock_busy' if result.get('errno') in (errno.EAGAIN, errno.EACCES) and result.get('reason') == 'BlockingIOError' else 'restore_failed'
    return 'retry', reason, stage
  try:
    status, raw = run_bounded(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value'], 5)
    if status == 0 and raw.strip() == b'active':
      return 'ready', '', 'runtime_unit_restore'
  except (OSError, subprocess.TimeoutExpired, ValueError):
    pass
  return 'retry', 'unit_not_active_yet', 'runtime_unit_restore'


def supervise():
  attempt = 0
  while True:
    try:
      enabled = configured()
    except FileNotFoundError:
      return 0  # Not enrolled or explicitly removed; do not recreate enrollment.
    except (OSError, ValueError) as error:
      reason = str(error) if str(error) in PERMANENT | {'unsafe_boot_metadata'} else 'qualification_unreadable'
      record('blocked', attempt, reason, 'boot_qualification')
      return 1
    if not enabled:
      record('disabled', attempt, 'not_enabled', 'boot_qualification')
      return 0
    attempt += 1
    began = time.monotonic()
    state, reason, stage = attempt_restore()
    record(state, attempt, reason, stage, time.monotonic() - began)
    if state in ('ready', 'disabled', 'blocked'):
      return 0 if state in ('ready', 'disabled') else 1
    time.sleep(min(60, 2 ** min(attempt, 6)))


def main():
  import fcntl
  if os.geteuid() != 0 or not Path('/TICI').exists():
    return 1
  protected(ROOT, directory=True)
  path = ROOT / 'boot_restore_supervisor.lock'
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
  with os.fdopen(fd, 'wb') as handle:
    protected(path)
    try:
      fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return 0
    return supervise()


if __name__ == '__main__':
  raise SystemExit(main())
