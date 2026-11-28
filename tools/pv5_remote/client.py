"""Owner-PC connection resilience; never replays a user command or changes device gates."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import time


def connector(policy, *, sidecar=False):
  cfg = policy['conditional_remote']
  if cfg.get('pc_enabled') is not True:
    raise ValueError('remote_pc_disabled')
  setting = 'park_code_root' if sidecar else 'code_root'
  if not cfg.get(setting):
    raise ValueError('park_code_root_required' if sidecar else 'code_root_required')
  root = Path(cfg[setting]) / 'tools/pv5_remote'
  # Keep the deployed, qualified guard pin independent of this PC-only client.
  return runpy.run_path(str(root / 'connect.py')), root


def command(policy, *, sidecar=False):
  module, _ = connector(policy, sidecar=sidecar)
  args = module['command'](policy, sidecar=True) if sidecar else module['command'](policy)
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


def check(policy, runner=subprocess.run, sleep=time.sleep, *, sidecar=False):
  module, root = connector(policy, sidecar=sidecar)
  parked = sidecar or policy.get('conditional_remote', {}).get('resource_policy') == 'parked_vehicle_capped_v1'
  if sidecar:
    probe, proof_args = module['sidecar_probe_arguments'](policy, False)
  else:
    probe = module['CHECK_PARK'] if parked else module['CHECK']
    expected = hashlib.sha256((root / 'conditional_remote.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
  args = command(policy, sidecar=sidecar) + [module['remote'](probe)]
  flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
  for attempt in range(1, 4):
    try:
      result = runner(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=75 if parked else 25, creationflags=flags)
      if result.returncode == 0:
        if len(result.stdout) > 8192:
          raise ValueError('invalid_remote_check')
        proof = json.loads(result.stdout)
        if sidecar and not module['valid_sidecar_proof'](proof, **proof_args):
          raise ValueError('sidecar_probe_mismatch')
        if not sidecar and proof.get('guard_sha256') != expected:
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
  parser.add_argument('--park-sidecar-check', action='store_true')
  args = parser.parse_args()
  policy = json.loads(args.policy.read_text(encoding='utf-8-sig'))
  print(json.dumps(check(policy, sidecar=args.park_sidecar_check)))


if __name__ == '__main__':
  main()
