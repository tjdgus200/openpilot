"""Owner PC key-only checks. No interactive login or untrusted incident commands."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import threading
import time

HOME_POLICY = 'detached_home_uncapped_v1'
PARK_POLICY = 'parked_vehicle_capped_v1'
SIDECAR_POLICY = 'parked_vehicle_memory_capped_v1'
PARK_MODES = ['parked_vehicle']

CHECK_ISOLATION = r'''
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
'''
CHECK = CHECK_ISOLATION + r'''
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

# Same owner-key, namespace and cancellation checks; vehicle limits are distinct.
CHECK_PARK = CHECK.replace(
  "assert config.get('qualified_modes')==['detached_home'] and config.get('resource_policy')=='detached_home_uncapped_v1','home_resource_policy'",
  "assert config.get('qualified_modes')==['parked_vehicle'] and config.get('resource_policy')=='parked_vehicle_capped_v1','park_resource_policy'"
)
_start = CHECK_PARK.index("assert (root/'memory.max')")
_end = CHECK_PARK.index('observed=[]', _start)
CHECK_PARK = CHECK_PARK[:_start] + "import runpy\nm=runpy.run_path('/data/pv5-remote-access/conditional_remote.py')\nlimits=m['resource_limits'](config)\nassert limits['memory_limit']==268435456 and limits['CPU_quota_limit']==0.1 and limits['tasks_limit']==64\nsource=m['Source']()\nadmitted=m['await_eligible'](source,config['qualified_modes'])\nhas_cpu=limits['CPU_controller'] is not None\n" + CHECK_PARK[_end:]
CHECK_PARK = CHECK_PARK.replace('observed.append(memory());time.sleep(.5)', 'observed.append(memory());source.read();time.sleep(.5)')
CHECK_PARK = CHECK_PARK.replace("digest=hashlib.sha256", "admitted=m['eligible'](source.read(),time.monotonic(),config['qualified_modes'])[0]\nassert admitted=='parked_vehicle','actual_park_proof_required'\ndigest=hashlib.sha256")
CHECK_PARK = CHECK_PARK.replace("'memory_limit':None", "'memory_limit':limits['memory_limit']")
CHECK_PARK = CHECK_PARK.replace("'CPU_quota_limit':None", "'CPU_quota_limit':limits['CPU_quota_limit'],'admitted_mode':admitted,'tasks_limit':limits['tasks_limit']")

JOB = r'''
import os,subprocess,time
child=subprocess.Popen(['/bin/sleep','600'])
rows=[{'pid':pid,'start':P('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[19]} for pid in (os.getpid(),child.pid)]
path=base/'conditional_trial_processes.json'
assert not path.is_symlink()
audit={'processes':rows}
if trial_id: audit['trial_id']=trial_id
with path.open('w') as f: os.chmod(path,0o600);json.dump(audit,f);f.flush();os.fsync(f.fileno())
print(json.dumps({'trial_job_ready':True}),flush=True)
time.sleep(600)
'''

STOP = r'''
(runtime/'stop-trial').touch()
if trial_id: print(json.dumps({'stop_requested':True}),flush=True)
'''


def park_target(cfg):
  try:
    address = ipaddress.IPv4Address(cfg.get('park_device_ip'))
    home = ipaddress.IPv4Address(cfg['device_ip'])
  except (KeyError, ValueError) as exc:
    raise ValueError('separate_enrolled_park_target_required') from exc
  if home == address or address not in ipaddress.ip_network('100.64.0.0/10'):
    raise ValueError('separate_enrolled_park_target_required')
  return address


def command(policy, *, sidecar=False):
  cfg = policy['conditional_remote']
  folder = Path(cfg['private_directory'])
  address = park_target(cfg) if sidecar else ipaddress.ip_address(cfg['device_ip'])
  if address not in ipaddress.ip_network('100.64.0.0/10') or folder.is_symlink():
    raise ValueError('invalid_owner_target')
  for name in ('client_ed25519', 'known_hosts', 'host_key.pub'):
    path = folder / name
    if not path.is_file() or path.is_symlink():
      raise ValueError('private_key_and_pinned_host_required')
  if sidecar:
    host_file, known = folder / 'host_key.pub', folder / 'known_hosts'
    if host_file.stat().st_size > 256 or known.stat().st_size > 512:
      raise ValueError('enrolled_host_pin_mismatch')
    host = host_file.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{68}', host) or known.read_text(encoding='ascii') != 'pv5-conditional-owner ' + host + '\n':
      raise ValueError('enrolled_host_pin_mismatch')
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
    *(['-o', 'IdentityAgent=none', '-o', 'ControlMaster=no', '-o', 'ControlPath=none'] if sidecar else []),
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


def park_client_digest(folder):
  from base64 import b64decode

  path = folder / 'client_ed25519.pub'
  if path.is_symlink() or not path.is_file() or path.stat().st_size > 512:
    raise ValueError('enrolled_public_key_required')
  fields = path.read_text(encoding='ascii').strip().split()
  if len(fields) < 2 or fields[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/]{68}', fields[1]):
    raise ValueError('enrolled_public_key_required')
  raw = b64decode(fields[1], validate=True)
  if not raw.startswith(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20') or len(raw) != 51:
    raise ValueError('enrolled_public_key_required')
  return hashlib.sha256(' '.join(fields[:2]).encode('ascii')).hexdigest()


def park_sidecar_check(expected, client_digest, target, *, registration=False):
  # The host key is already pinned by SSH. Match the installed guard before importing it.
  for digest in (expected, client_digest):
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
      raise ValueError('invalid_digest')
  target = str(ipaddress.IPv4Address(target))
  isolation = CHECK_ISOLATION.replace('/pv5-conditional-remote.service', '/pv5-park-remote.service')
  bindings = f'expected={expected!r}\nclient_digest={client_digest!r}\ntarget={target!r}\nregistration={registration!r}\n'
  return "if not __debug__: raise RuntimeError('probe_assertions_required')\n" + isolation + bindings + r'''
import re,runpy,sys
base=P('/data/pv5-remote-access/park')
guard=base/'conditional_remote.py'
assert hashlib.sha256(guard.read_bytes().replace(b'\r\n',b'\n')).hexdigest()==expected,'guard_code_identity'
m=runpy.run_path(str(guard))
config=json.loads((base/'conditional_modes.json').read_bytes())
assert config.get('qualified_modes')==['parked_vehicle'] and config.get('resource_policy') in ('parked_vehicle_memory_capped_v1','parked_vehicle_capped_v1'),'park_resource_policy'
trial=config.get('trial')
assert type(trial) is bool and (not registration or trial),'registration_requires_trial'
trial_id=config.get('trial_id') if trial else None
assert not trial or isinstance(trial_id,str) and re.fullmatch('[0-9a-f]{32}',trial_id),'invalid_trial_id'
limits=m['resource_limits'](config)
auth=P('/run/pv5-park-remote/authorized_keys').read_text().splitlines()
assert len(auth)==1 and len(auth[0].split())==3,'enrolled_public_key_required'
options,kind,material=auth[0].split()
assert options=='no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc','enrolled_key_restrictions'
active=m['public_key'](kind+' '+material)
assert hashlib.sha256(active.encode('ascii')).hexdigest()==client_digest,'enrolled_owner_key_changed'
node=m['run']([str(m['BIN']/'tailscale'),'--socket='+str(m['SOCKET']),'ip','-4']).decode('ascii').strip()
assert node==target,'unexpected_overlay_identity'
sys.path.insert(0,'/data/openpilot')
source=m['Source']()
m['await_eligible'](source,config['qualified_modes'],registration=registration)
observed=[]
for i in range(6):
 admitted=m['eligible'](source.read(),time.monotonic(),config['qualified_modes'],registration=registration)[0]
 assert admitted=='parked_vehicle','actual_park_proof_required'
 observed.append(memory())
 time.sleep(.1)
print(json.dumps(dict(limits,root_key_session=True,guard_cgroup=True,host_service_isolation=True,
 observed_memory_bytes=max(observed),qualified_modes=config['qualified_modes'],admitted_mode=admitted,
 trial=trial,trial_id=trial_id,guard_sha256=expected,client_key_sha256=client_digest,target_ip=node)))
'''


def valid_sidecar_proof(proof, *, guard_digest, client_digest, target, registration):
  if proof.get('resource_policy') == PARK_POLICY:
    limits = (proof.get('CPU_controller') in ('v1', 'v2') and proof.get('CPU_quota_limit') == 0.1
              and type(proof.get('tasks_limit')) is int and proof['tasks_limit'] == 64)
  elif proof.get('resource_policy') == SIDECAR_POLICY:
    limits = valid_memory_profile(proof)
  else:
    return False
  return (limits and type(proof.get('memory_limit')) is int and proof['memory_limit'] == 268435456
          and type(proof.get('observed_memory_bytes')) is int and 0 < proof['observed_memory_bytes'] <= proof['memory_limit']
          and proof.get('guard_sha256') == guard_digest and proof.get('client_key_sha256') == client_digest
          and proof.get('target_ip') == str(target) and proof.get('qualified_modes') == PARK_MODES
          and proof.get('admitted_mode') == 'parked_vehicle'
          and all(proof.get(key) is True for key in ('root_key_session', 'guard_cgroup', 'host_service_isolation'))
          and type(proof.get('trial')) is bool
          and (not registration or (proof['trial'] and isinstance(proof.get('trial_id'), str)
                                    and re.fullmatch('[0-9a-f]{32}', proof['trial_id']) is not None)))


def valid_memory_profile(proof):
  cpus = proof.get('cpu_affinity')
  return (
    isinstance(cpus, list) and 0 < len(cpus) <= 4 and all(type(cpu) is int and 0 <= cpu <= 3 for cpu in cpus)
    and cpus == sorted(set(cpus)) and proof.get('no_new_privs') is True
    and all(key in proof and proof[key] is None for key in ('CPU_quota_limit', 'CPU_controller', 'tasks_limit'))
    and all(type(proof.get(key)) is int and proof[key] == value for key, value in (
      ('nice', 19), ('oom_score_adj', 500), ('nofile_limit', 256), ('core_limit', 0)))
  )


def trial_command(action, *, sidecar=False, trial_id=None, policy=None):
  if sidecar:
    if not isinstance(trial_id, str) or not re.fullmatch('[0-9a-f]{32}', trial_id):
      raise ValueError('invalid_trial_id')
    if policy not in (PARK_POLICY, SIDECAR_POLICY):
      raise ValueError('invalid_sidecar_policy')
  unit = 'pv5-park-remote' if sidecar else 'pv5-conditional-remote'
  base = '/data/pv5-remote-access/park' if sidecar else '/data/pv5-remote-access'
  return f'''import json,pathlib
if not __debug__: raise RuntimeError('probe_assertions_required')
P=pathlib.Path
group=next(v.split(':',2)[2] for v in P('/proc/self/cgroup').read_text().splitlines() if v.startswith('0::'))
assert group.endswith('/{unit}.service'),'guard_cgroup'
base=P('{base}')
runtime=P('/run/{unit}')
config=json.loads((base/'conditional_modes.json').read_bytes())
assert config.get('trial') is True,'matching_mode_trial_required'
trial_id={trial_id!r}
if trial_id: assert config.get('trial_id')==trial_id and config.get('resource_policy')=={policy!r},'matching_mode_trial_required'
''' + action


def sidecar_probe_arguments(policy, registration):
  cfg = policy['conditional_remote']
  target = park_target(cfg)
  if not cfg.get('park_code_root'):
    raise ValueError('park_code_root_required')
  expected = hashlib.sha256((Path(cfg['park_code_root']) / 'tools/pv5_remote/conditional_remote.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
  client_digest = park_client_digest(Path(cfg['private_directory']))
  proof_args = dict(guard_digest=expected, client_digest=client_digest, target=target, registration=registration)
  return park_sidecar_check(expected, client_digest, target, registration=registration), proof_args


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--policy', type=Path, required=True)
  mode = parser.add_mutually_exclusive_group()
  mode.add_argument('--verify-home', action='store_true')
  mode.add_argument('--verify-park', action='store_true')
  mode.add_argument('--park-check', action='store_true')
  mode.add_argument('--verify-park-sidecar', action='store_true')
  mode.add_argument('--park-sidecar-check', action='store_true')
  args = parser.parse_args()
  policy = json.loads(args.policy.read_text(encoding='utf-8'))
  if policy.get('conditional_remote', {}).get('pc_enabled') is not True:
    raise ValueError('remote_pc_disabled')
  sidecar = args.verify_park_sidecar or args.park_sidecar_check
  expected_policy = SIDECAR_POLICY if sidecar else PARK_POLICY if (args.verify_park or args.park_check) else HOME_POLICY
  parked = expected_policy != HOME_POLICY
  expected_modes = PARK_MODES if parked else ['detached_home']
  ssh, folder = command(policy, sidecar=sidecar), Path(policy['conditional_remote']['private_directory'])
  flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
  if sidecar:
    probe, proof_args = sidecar_probe_arguments(policy, args.verify_park_sidecar)
  else:
    probe = CHECK_PARK if parked else CHECK
  result = subprocess.run(ssh + [remote(probe)], stdin=subprocess.DEVNULL, capture_output=True, timeout=70 if parked else 15, creationflags=flags)
  if result.returncode or len(result.stdout) > 8192:
    raise ValueError('key_auth_or_resource_check_failed')
  proof = json.loads(result.stdout)
  if sidecar:
    if not valid_sidecar_proof(proof, **proof_args):
      raise ValueError('sidecar_probe_mismatch')
    expected_policy = proof['resource_policy']
  elif proof.get('guard_sha256') != hashlib.sha256(Path(__file__).with_name('conditional_remote.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest():
    raise ValueError('guard_code_identity')
  if not (args.verify_home or args.verify_park or args.verify_park_sidecar):
    print(json.dumps(proof))
    return
  if proof.get('trial') is not True or proof.get('qualified_modes') != expected_modes or proof.get('resource_policy') != expected_policy:
    raise ValueError('matching_mode_trial_required')
  trial_args = {'sidecar': True, 'trial_id': proof['trial_id'], 'policy': expected_policy} if sidecar else {}
  job = trial_command(JOB, **trial_args)
  stop = trial_command(STOP, **trial_args)
  client = subprocess.Popen(ssh + [remote(job)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
  try:
    ready = queue.Queue(maxsize=1)

    def read_ready():
      ready.put(client.stdout.readline(8193))

    threading.Thread(target=read_ready, daemon=True).start()
    if json.loads(ready.get(timeout=10)).get('trial_job_ready') is not True:
      raise ValueError('trial_job_failed')
    began = time.monotonic()
    stopped = subprocess.run(ssh + [remote(stop)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, creationflags=flags)
    if sidecar and (stopped.returncode not in (0, 255) or stopped.stdout.strip() != b'{"stop_requested": true}'):
      raise ValueError('sidecar_stop_unconfirmed')
    exit_status = client.wait(timeout=5)
    if sidecar and exit_status == 0:
      raise ValueError('sidecar_job_cancellation_unconfirmed')
    try:
      closed = subprocess.run(ssh + ['true'], stdin=subprocess.DEVNULL, capture_output=True, timeout=8, creationflags=flags)
      if closed.returncode == 0:
        raise ValueError('trial_transport_remained_open')
      if sidecar and (closed.returncode != 255 or not any(reason in closed.stderr.lower() for reason in
                     (b'connection refused', b'connection reset', b'connection closed by'))):
        raise ValueError('sidecar_reconnect_refusal_unconfirmed')
    except subprocess.TimeoutExpired:
      if sidecar:
        raise ValueError('sidecar_reconnect_refusal_unconfirmed') from None
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
    path = folder / ('park-sidecar-qualification.json' if sidecar else 'park-qualification.json' if args.verify_park else 'home-qualification.json')
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
