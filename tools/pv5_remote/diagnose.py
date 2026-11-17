#!/usr/bin/env python3
"""One-shot local read-only diagnosis. No service actions, secrets or raw logs."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

ROOT = Path('/data/pv5-remote-access')
UNIT = 'pv5-conditional-remote.service'
SAFE_ERRORS = {'PermissionError', 'FileNotFoundError', 'TimeoutExpired', 'ModuleNotFoundError', 'ImportError', 'ValueError', 'OSError'}
PROCESSES = ('pandad', 'hardwared', 'card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd')


def error_name(error):
  return type(error).__name__ if type(error).__name__ in SAFE_ERRORS else 'diagnostic_failed'


def query(args, timeout=4):
  result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
  if result.returncode or len(result.stdout) > 65536:
    raise ValueError('query_failed')
  return result.stdout


def protected_snapshot():
  out = {}
  fields = ('LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'NRestarts')
  allowed = {'loaded', 'not-found', 'masked', 'active', 'inactive', 'failed', 'activating', 'deactivating',
             'running', 'dead', 'auto-restart', 'start', 'stop', 'exited', 'success', 'exit-code', 'signal', 'watchdog', 'timeout'}
  try:
    pairs = dict(line.split('=', 1) for line in query(['systemctl', 'show', UNIT] + [arg for f in fields for arg in ('-p', f)]).decode().splitlines() if '=' in line)
    out['unit'] = {k: v if v in allowed or (v.isascii() and v.isdecimal() and len(v) <= 10) else 'other' for k, v in pairs.items() if k in fields}
  except Exception as error:
    out['unit_error'] = error_name(error)
  try:
    config_path, script = ROOT / 'conditional_modes.json', ROOT / 'conditional_remote.py'
    if config_path.stat().st_size > 4096 or script.stat().st_size > 131072:
      raise ValueError('size')
    cfg = json.loads(config_path.read_bytes())
    m = runpy.run_path(str(script))
    out['qualification'] = {
      'boot_enabled': cfg.get('boot_restore') is True,
      'trial': cfg.get('trial') is True,
      'home_only': cfg.get('qualified_modes') == ['detached_home'],
      'code_matches': cfg.get('guard_sha256') == hashlib.sha256(script.read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
      'runtime_unit_present': Path('/run/systemd/system', UNIT).exists(),
      'trial_dropin_present': Path('/run/systemd/system', UNIT + '.d/trial.conf').exists(),
      'boot_hook_present': m['BOOT_HOOK'].encode() in Path('/data/openpilot/launch_chffrplus.sh').read_bytes().replace(b'\r\n', b'\n'),
      'unit_matches_interpreter': (ROOT / UNIT).read_bytes() == m['unit_text'](sys.executable).encode(),
      'binary_metadata_matches': cfg.get('binaries') == m['prerequisites'](),
    }
    sock = Path(m['SOCKET'])
    out['daemon_socket_present'] = sock.exists()
    if sock.exists():
      d = json.loads(query([str(m['BIN'] / 'tailscale'), '--socket=' + str(sock), 'status', '--json']))
      out['backend'] = d.get('BackendState') if d.get('BackendState') in ('Running', 'Stopped', 'NeedsLogin', 'NeedsMachineAuth', 'Starting', 'NoState') else 'other'
      out['node_online'] = d.get('Self', {}).get('Online') is True
  except Exception as error:
    out['protected_error'] = error_name(error)
  return out


def source_snapshot():
  # Never create IPC readers as root: preserve the normal comma user's IPC ownership.
  if os.geteuid() == 0:
    return {'source_error': 'run_as_normal_terminal_user'}
  out = {}
  try:
    m = runpy.run_path(str(ROOT / 'conditional_remote.py'))
    source = m['Source']()
    reasons = set()
    end = time.monotonic() + 3
    while time.monotonic() < end:
      samples = source.read()
      mode, reason = m['eligible'](samples, time.monotonic(), ['detached_home'])
      if reason in ('source_stale', 'resource', 'producer', 'not_detached', 'vehicle_source_stale', 'identity',
                    'normal_mode_code_reports_only', 'not_parked', 'control_or_emergency', 'mode_not_qualified', ''):
        reasons.add(reason)
      time.sleep(.1)
    now = time.monotonic()
    out['installed_gate'] = {'mode': mode if mode in ('detached_home', 'parked_dashcam', '') else 'other',
                             'reason': reason if reason in reasons else 'other', 'observed_reasons': sorted(reasons)}
    out['sources'] = {name: {'valid': samples.get(name, {}).get('valid') is True,
                            'age_ms': round((now - samples[name]['stamp']) * 1000) if name in samples else None}
                      for name in ('deviceState', 'pandaStates', 'managerState')}
    ds, ps, ms = (samples.get(name, {}) for name in ('deviceState', 'pandaStates', 'managerState'))
    out['started'] = ds.get('started') if type(ds.get('started')) is bool else None
    out['disconnected'] = ps.get('disconnected') if type(ps.get('disconnected')) is bool else None
    out['thermal_green'] = ds.get('thermal') == 0
    out['processes'] = {name: ms.get('processes', {}).get(name) for name in PROCESSES}
  except Exception as error:
    out['source_error'] = error_name(error)
  return out


def main():
  if sys.argv[1:] == ['--protected']:
    result = protected_snapshot()
  elif sys.argv[1:]:
    result = {'error': 'unsupported_arguments'}
  else:
    result = source_snapshot()
    # Only the bounded metadata child runs elevated; it never subscribes to IPC.
    try:
      child = [sys.executable, '-B', str(Path(__file__).resolve()), '--protected']
      if os.geteuid() != 0:
        child = ['sudo', '-n'] + child
      result['protected'] = json.loads(query(child, timeout=15))
    except Exception as error:
      result['protected'] = {'error': error_name(error)}
  print('PV5_REMOTE_DIAG ' + json.dumps(result, separators=(',', ':')) if sys.argv[1:] != ['--protected'] else json.dumps(result))


if __name__ == '__main__':
  main()
