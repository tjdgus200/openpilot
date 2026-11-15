"""Owner PC key-only checks. No interactive login or untrusted incident commands."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time

HOME_POLICY = 'detached_home_uncapped_v1'

CHECK = r'''
import errno,hashlib,json,os,pathlib,socket,time
P=pathlib.Path
group=next(v.split(':',2)[2] for v in P('/proc/self/cgroup').read_text().splitlines() if v.startswith('0::'))
assert group.endswith('/pv5-conditional-remote.service'),'guard_cgroup'
assert os.geteuid()==0,'root_key_session'
root=P('/sys/fs/cgroup')/group.lstrip('/')
try:
 fd=os.open(root/'cgroup.procs',os.O_WRONLY);os.close(fd);readonly=False
except OSError as e: readonly=e.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
def inaccessible(path):
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(.1)
 try: s.connect(path);return False
 except OSError as e: return e.errno in (errno.ENOENT,errno.EACCES,errno.EPERM,errno.ECONNREFUSED)
 finally: s.close()
blocked=all(inaccessible(v) for v in ('/run/dbus/system_bus_socket','/run/systemd/private'))
assert readonly and blocked,('host_service_isolation',readonly,blocked)
memory=lambda:int((root/'memory.current').read_text())
config=json.loads(P('/data/pv5-remote-access/conditional_modes.json').read_bytes())
assert config.get('qualified_modes')==['detached_home'] and config.get('resource_policy')=='detached_home_uncapped_v1','home_resource_policy'
assert (root/'memory.max').read_text().strip()=='max','unexpected_memory_cap'
has_cpu=(root/'cpu.max').exists()
assert not has_cpu or (root/'cpu.max').read_text().split()[0]=='max','unexpected_CPU_cap'
observed=[]
for i in range(6): observed.append(memory());time.sleep(.5)
digest=hashlib.sha256(P('/data/pv5-remote-access/conditional_remote.py').read_bytes().replace(b'\r\n',b'\n')).hexdigest()
print(json.dumps({'root_key_session':True,'guard_cgroup':True,'host_service_isolation':True,
 'resource_policy':config['resource_policy'],'observed_memory_bytes':max(observed),'memory_limit':None,
 'CPU_quota_limit':None,'CPU_controller_available':has_cpu,
 'qualified_modes':config['qualified_modes'],'trial':config.get('trial') is True,'guard_sha256':digest}))
'''

JOB = r'''
import json,os,pathlib,subprocess,time
P=pathlib.Path
group=next(v.split(':',2)[2] for v in P('/proc/self/cgroup').read_text().splitlines() if v.startswith('0::'))
assert group.endswith('/pv5-conditional-remote.service')
assert json.loads(P('/data/pv5-remote-access/conditional_modes.json').read_bytes()).get('trial') is True
child=subprocess.Popen(['/bin/sleep','600'])
rows=[{'pid':pid,'start':P('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[19]} for pid in (os.getpid(),child.pid)]
path=P('/data/pv5-remote-access/conditional_trial_processes.json')
assert not path.is_symlink()
with path.open('w') as f: os.chmod(path,0o600);json.dump({'processes':rows},f);f.flush();os.fsync(f.fileno())
print(json.dumps({'trial_job_ready':True}),flush=True)
time.sleep(600)
'''

STOP = r'''
import json,pathlib
P=pathlib.Path
assert next(v.split(':',2)[2] for v in P('/proc/self/cgroup').read_text().splitlines() if v.startswith('0::')).endswith('/pv5-conditional-remote.service')
assert json.loads(P('/data/pv5-remote-access/conditional_modes.json').read_bytes()).get('trial') is True
P('/run/pv5-conditional-remote/stop-trial').touch()
'''


def command(policy):
  cfg = policy['conditional_remote']
  folder = Path(cfg['private_directory'])
  address = ipaddress.ip_address(cfg['device_ip'])
  if address not in ipaddress.ip_network('100.64.0.0/10') or folder.is_symlink():
    raise ValueError('invalid_owner_target')
  for name in ('client_ed25519', 'known_hosts', 'host_key.pub'):
    path = folder / name
    if not path.is_file() or path.is_symlink():
      raise ValueError('private_key_and_pinned_host_required')
  return [
    cfg['ssh_executable'],
    '-F',
    'none',
    '-p',
    '2222',
    '-i',
    str(folder / 'client_ed25519'),
    '-o',
    'BatchMode=yes',
    '-o',
    'IdentitiesOnly=yes',
    '-o',
    'PreferredAuthentications=publickey',
    '-o',
    'PasswordAuthentication=no',
    '-o',
    'KbdInteractiveAuthentication=no',
    '-o',
    'StrictHostKeyChecking=yes',
    '-o',
    'GlobalKnownHostsFile=none',
    '-o',
    'UserKnownHostsFile=' + str(folder / 'known_hosts'),
    '-o',
    'HostKeyAlias=pv5-conditional-owner',
    '-o',
    'UpdateHostKeys=no',
    '-o',
    'ConnectTimeout=5',
    '-o',
    'ServerAliveInterval=2',
    '-o',
    'ServerAliveCountMax=1',
    'root@' + str(address),
  ]


def remote(code):
  return '/usr/local/venv/bin/python3 -u -c ' + shlex.quote(code)


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--policy', type=Path, required=True)
  parser.add_argument('--verify-home', action='store_true')
  args = parser.parse_args()
  policy = json.loads(args.policy.read_text(encoding='utf-8'))
  if policy.get('conditional_remote', {}).get('pc_enabled') is not True:
    raise ValueError('remote_pc_disabled')
  ssh, folder = command(policy), Path(policy['conditional_remote']['private_directory'])
  flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
  result = subprocess.run(ssh + [remote(CHECK)], stdin=subprocess.DEVNULL, capture_output=True, timeout=15, creationflags=flags)
  if result.returncode or len(result.stdout) > 8192:
    raise ValueError('key_auth_or_resource_check_failed')
  proof = json.loads(result.stdout)
  expected = hashlib.sha256(Path(__file__).with_name('conditional_remote.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
  if proof.get('guard_sha256') != expected:
    raise ValueError('guard_code_identity')
  if not args.verify_home:
    print(json.dumps(proof))
    return
  if proof.get('trial') is not True or proof.get('qualified_modes') != ['detached_home'] or proof.get('resource_policy') != HOME_POLICY:
    raise ValueError('home_trial_required')
  client = subprocess.Popen(ssh + [remote(JOB)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
  try:
    ready = queue.Queue(maxsize=1)

    def read_ready():
      ready.put(client.stdout.readline(8193))

    threading.Thread(target=read_ready, daemon=True).start()
    if json.loads(ready.get(timeout=10)).get('trial_job_ready') is not True:
      raise ValueError('trial_job_failed')
    began = time.monotonic()
    subprocess.run(ssh + [remote(STOP)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, creationflags=flags)
    client.wait(timeout=5)
    try:
      closed = subprocess.run(ssh + ['true'], stdin=subprocess.DEVNULL, capture_output=True, timeout=8, creationflags=flags)
      if closed.returncode == 0:
        raise ValueError('trial_transport_remained_open')
    except subprocess.TimeoutExpired:
      pass  # A stopped transport may time out; this is not an authenticated session.
    receipt = dict(
      proof,
      passed=True,
      stamp=time.time(),
      host_key=(folder / 'host_key.pub').read_text().strip(),
      transport_closed=True,
      local_pid_audit_pending=True,
      stop_and_rejection_seconds=round(time.monotonic() - began, 3),
    )
    path = folder / 'home-qualification.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt) + '\n', encoding='utf-8')
    temporary.replace(path)
    # Do not print public/private authentication material in chat or logs.
    print(json.dumps({key: value for key, value in receipt.items() if key != 'host_key'}))
  finally:
    if client.poll() is None:
      client.kill()
      client.wait()


if __name__ == '__main__':
  try:
    main()
  except Exception as error:
    reason = str(error) if isinstance(error, ValueError) and str(error).replace('_', '').isalpha() else type(error).__name__
    print(json.dumps({'success': False, 'reason': reason}))
    raise SystemExit(1) from None
