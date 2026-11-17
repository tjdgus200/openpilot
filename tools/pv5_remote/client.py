"""Owner-PC connection resilience; never replays a user command or changes device gates."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import time


def connector(policy):
  cfg = policy['conditional_remote']
  if cfg.get('pc_enabled') is not True:
    raise ValueError('remote_pc_disabled')
  root = Path(cfg['code_root']) / 'tools/pv5_remote'
  # Keep the deployed, qualified guard pin independent of this PC-only client.
  return runpy.run_path(str(root / 'connect.py')), root


def command(policy):
  module, _ = connector(policy)
  args = module['command'](policy)
  options = {'serveraliveinterval', 'serveralivecountmax', 'connectionattempts'}
  kept, index = [], 0
  while index < len(args):
    if args[index] == '-o' and index + 1 < len(args) and args[index + 1].split('=', 1)[0].lower() in options:
      index += 2
    else:
      kept.append(args[index]); index += 1
  # Retries only TCP establishment, never a command after authentication.
  return kept[:-1] + ['-o', 'ConnectionAttempts=3', '-o', 'ServerAliveInterval=5',
                      '-o', 'ServerAliveCountMax=6'] + kept[-1:]


def check(policy, runner=subprocess.run, sleep=time.sleep):
  module, root = connector(policy)
  args = command(policy) + [module['remote'](module['CHECK'])]
  expected = hashlib.sha256((root / 'conditional_remote.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
  flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
  for attempt in range(1, 4):
    try:
      result = runner(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=25, creationflags=flags)
      if result.returncode == 0:
        if len(result.stdout) > 8192:
          raise ValueError('invalid_remote_check')
        proof = json.loads(result.stdout)
        if proof.get('guard_sha256') != expected:
          raise ValueError('guard_code_identity')
        if not all(proof.get(k) is True for k in ('root_key_session', 'guard_cgroup', 'host_service_isolation')):
          raise ValueError('remote_check_failed')
        return {'connected': True, 'attempts': attempt, 'trial': proof.get('trial'),
                'qualified_modes': proof.get('qualified_modes'), 'guard_identity_verified': True}
      # Do not retry authentication, host identity failures or remote execution failures.
      error = result.stderr.lower()
      if result.returncode != 255 or any(s in error for s in
          (b'permission denied', b'host key verification failed', b'host identification has changed',
           b'unprotected private key', b'load key', b'bad permissions')):
        raise ValueError('authentication_identity_or_remote_check_failed')
    except subprocess.TimeoutExpired:
      pass  # This CHECK is read-only; no arbitrary operation is retried.
    if attempt < 3:
      sleep(2 ** attempt)
  raise ValueError('remote_temporarily_unavailable')


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--policy', type=Path, required=True)
  args = parser.parse_args()
  policy = json.loads(args.policy.read_text(encoding='utf-8-sig'))
  print(json.dumps(check(policy)))


if __name__ == '__main__':
  main()
