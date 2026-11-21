#!/usr/bin/env python3
"""Conditional owner remote access. Local one-time installation; no control writes."""

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import socket
import stat
import subprocess
import sys
import threading
import time

ROOT = Path('/data/pv5-remote-access')
SCRIPT = ROOT / 'conditional_remote.py'
UNIT = 'pv5-conditional-remote.service'
RUNTIME = Path('/run/pv5-conditional-remote')
RUNTIME_UNITS = Path('/run/systemd/system')
STAGE = 'entry'
HOME_POLICY = 'detached_home_uncapped_v1'
PARK_POLICY = 'parked_vehicle_capped_v1'
PARK_MODES = ['parked_vehicle']
BOOT_HOOK = '''  # PV5_CONDITIONAL_REMOTE_RUNTIME_RESTORE_V2
  if [ -f /TICI ] && [ -f /data/pv5-remote-access/conditional_modes.json ] && [ -f /data/pv5-remote-access/conditional_remote.py ]; then
    (
      exec 9>&-
      unset CARROT_BOOT_LOCK_FD
      sudo -n env PYTHONPATH="$PYTHONPATH" PYTHONDONTWRITEBYTECODE=1 \\
        nice -n 19 python3 "$DIR/tools/pv5_remote/restore_supervisor.py" >/dev/null 2>&1
    ) </dev/null >/dev/null 2>&1 &
  fi
'''
SOCKET = str(RUNTIME / 'tailscaled.sock')
BIN = ROOT / '1.104.5'
SERVICES = ('deviceState', 'pandaStates', 'managerState', 'carParams', 'carState', 'carControl', 'selfdriveState', 'onroadEvents')
BINARY_SHA = {
  'tailscale': 'a000d3ee4734531da8e0b2874453e01c6f841e6fea9edae8621f4b2e4e45ab48',
  'tailscaled': 'd1532cdbbe9c9a86275939b9130e837da8bb7223d2647996b7b1fabf48bc8359',
}


def encode(value):
  return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def mark(stage):
  global STAGE
  STAGE = stage


def public_key(value):
  if not isinstance(value, str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{68}', value):
    raise ValueError('invalid_public_key')
  raw = base64.b64decode(value.split()[1], validate=True)
  if len(raw) != 51 or raw[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
    raise ValueError('invalid_public_key')
  return value


def finite(value):
  return type(value) in (int, float) and math.isfinite(value)


def home_policy(config):
  if config.get('qualified_modes') != ['detached_home'] or config.get('resource_policy') != HOME_POLICY:
    raise ValueError('home_resource_policy_required')


def access_policy(config):
  if config.get('resource_policy') == PARK_POLICY and config.get('qualified_modes') == PARK_MODES:
    return PARK_POLICY
  home_policy(config)
  return HOME_POLICY


def resource_limits(config, proc=Path('/proc/self/cgroup'), cgroups=Path('/sys/fs/cgroup')):
  """Read actual kernel limits, never infer enforcement from unit directives."""
  policy = access_policy(config)
  groups = [line.split(':', 2) for line in proc.read_text().splitlines()]
  group = next((v[2] for v in groups if v[0] == '0' and v[1] == ''), '')
  if not group.endswith('/' + UNIT) or '..' in group.split('/'):
    raise ValueError('guard_cgroup_required')
  base = cgroups / group.lstrip('/')
  memory = (base / 'memory.max').read_text().strip()
  cpu = base / 'cpu.max'
  controller = 'v2' if cpu.exists() else None
  quota, period = cpu.read_text().split() if cpu.exists() else ('max', '100000')
  if policy == HOME_POLICY:
    if memory != 'max' or quota != 'max':
      raise ValueError('unexpected_home_resource_cap')
    return {'resource_policy': policy, 'memory_limit': None, 'CPU_quota_limit': None, 'CPU_controller': controller}
  if not cpu.exists():
    # Existing hybrid mount only: never mount/reconfigure the host hierarchy.
    cpu_group = next((v[2] for v in groups if 'cpu' in v[1].split(',')), '')
    if cpu_group != group:
      raise ValueError('vehicle_cpu_controller_required')
    for mount in ('cpu', 'cpu,cpuacct', 'cpuacct,cpu'):
      candidate = cgroups / mount / group.lstrip('/')
      if (candidate / 'cpu.cfs_quota_us').is_file():
        quota = (candidate / 'cpu.cfs_quota_us').read_text().strip()
        period = (candidate / 'cpu.cfs_period_us').read_text().strip()
        controller = 'v1'
        break
  if not (memory.isdecimal() and 0 < int(memory) <= 268435456):
    raise ValueError('vehicle_memory_limit_required')
  if not (quota.isdecimal() and period.isdecimal() and 1000 <= int(period) <= 1000000 and 0 < int(quota) * 10 <= int(period)):
    raise ValueError('vehicle_cpu_limit_required')
  tasks = (base / 'pids.max').read_text().strip()
  if not tasks.isdecimal() or not 0 < int(tasks) <= 64:
    raise ValueError('vehicle_tasks_limit_required')
  return {'resource_policy': policy, 'memory_limit': int(memory), 'CPU_quota_limit': int(quota) / int(period),
          'CPU_controller': controller, 'tasks_limit': int(tasks)}


def eligible(samples, now, modes):
  def fresh(name, ttl):
    value = samples.get(name, {})
    return value.get('valid') is True and finite(value.get('stamp')) and 0 <= now - value['stamp'] < ttl

  if not finite(now) or not all(fresh(name, ttl) for name, ttl in (('deviceState', 1.5), ('pandaStates', 0.25), ('managerState', 1.5))):
    return '', 'source_stale'
  ds, ps, ms = (samples[name] for name in ('deviceState', 'pandaStates', 'managerState'))
  if ds.get('thermal') != 0:
    return '', 'resource'
  processes = ms['processes']
  if any(processes.get(name) != [True, True] for name in ('pandad', 'hardwared')):
    return '', 'producer'
  # A running model/recorder is not proof of a vehicle connection. While
  # started, require at least one real board explicitly reporting no harness
  # and no ignition; an empty board list must not bypass the vehicle gates.
  board_count = ps.get('board_count')
  detached = ps.get('disconnected') is True and (
    ds['started'] is False or (ds['started'] is True and type(board_count) is int and 1 <= board_count <= 8)
  )
  if detached:
    mode = 'detached_home'
  elif ds['started'] is False:
    return '', 'not_detached'
  else:
    usage = ds.get('cpu', [])
    if (
      not usage
      or len(usage) > 32
      or not all(finite(value) and 0 <= value <= 70 for value in usage)
      or not finite(ds.get('memory'))
      or not 0 <= ds['memory'] <= 80
      or not finite(ds.get('free'))
      or not 20 <= ds['free'] <= 100
    ):
      return '', 'resource'
    if not all(fresh(name, 0.25) for name in ('carState', 'carControl', 'selfdriveState')) or not fresh('onroadEvents', 2.5):
      return '', 'vehicle_source_stale'
    cp = samples.get('carParams', {})
    if cp.get('valid') is not True or cp.get('vehicle') != 'KIA_PV5' or cp.get('notCar') is not False:
      return '', 'identity'
    if cp.get('dashcam') is not True and 'parked_vehicle' not in modes:
      return '', 'normal_mode_code_reports_only'
    cs, cc, sd = (samples[name] for name in ('carState', 'carControl', 'selfdriveState'))
    if 'parked_vehicle' in modes and any(value is not False for value in (cs.get('cruise'), cs.get('aeb'), cc.get('enabled'), cc.get('lat'), cc.get('long'), sd.get('enabled'), sd.get('active'), sd.get('critical'), samples['onroadEvents'].get('disable'))):
      return '', 'control_or_emergency'
    if (
      cs['can_valid'] is not True
      or cs['can_timeout'] is not False
      or cs['standstill'] is not True
      or cs['gear'] != 'park'
      or not finite(cs['raw_speed'])
      or cs['raw_speed'] != 0
      or not finite(cs['speed'])
      or (cs['speed'] != 0 if 'parked_vehicle' in modes else abs(cs['speed']) > 0.01)
    ):
      return '', 'not_parked'
    if (
      cs['cruise']
      or cs['aeb']
      or cc['enabled']
      or cc['lat']
      or cc['long']
      or sd['enabled']
      or sd['active']
      or sd['state'] != 'disabled'
      or sd['critical']
      or samples['onroadEvents']['disable']
    ):
      return '', 'control_or_emergency'
    mode = 'parked_vehicle' if 'parked_vehicle' in modes else 'parked_dashcam'
  return (mode, '') if mode in modes else ('', 'mode_not_qualified')


class Gate:
  def __init__(self):
    self.last, self.since, self.mode, self.opened = None, None, '', False

  def tick(self, mode, now):
    if not finite(now) or (self.last is not None and (now < self.last or now - self.last >= (0.25 if self.opened else 0.75))):
      mode = ''
    self.last = now
    if not mode or mode != self.mode:
      self.since, self.mode = None, mode
      if self.opened:
        return 'close'
    if not mode:
      return 'wait'
    if self.since is None:
      self.since = now
    if not self.opened and now - self.since >= 2.0:
      self.opened = True
      return 'open'
    return 'keep' if self.opened else 'wait'


class Source:
  def __init__(self):
    if any(os.environ.get(key) for key in ('SIMULATION', 'REPLAY', 'OPENPILOT_PREFIX', 'ZMQ')):
      raise ValueError('real_local_sources_required')
    from openpilot.cereal import log, messaging

    self.log = log
    self.readers = {name: messaging.sub_sock(name, conflate=True, timeout=0) for name in SERVICES}
    self.samples = {}

  def read(self):
    for name, reader in self.readers.items():
      raw = reader.receive(non_blocking=True)
      if raw is None:
        continue
      if len(raw) > (1048576 if name == 'carParams' else 65536):
        raise ValueError('source_size')
      with self.log.Event.from_bytes(raw, traversal_limit_in_words=131072) as event:
        if event.which() != name:
          raise ValueError('source_type')
        state = getattr(event, name)
        value = {'stamp': event.logMonoTime / 1e9, 'valid': event.valid}
        previous = self.samples.get(name)
        if previous and value['stamp'] < previous['stamp']:
          raise ValueError('source_time_regressed')
        if name == 'deviceState':
          value.update(
            started=state.started,
            cpu=list(state.cpuUsagePercent),
            thermal=int(state.thermalStatus.raw),
            memory=state.memoryUsagePercent,
            free=state.freeSpacePercent,
          )
        elif name == 'pandaStates':
          if len(state) > 8:
            raise ValueError('source_size')
          value['board_count'] = len(state)
          value['disconnected'] = all(
            str(board.pandaType) != 'unknown' and str(board.harnessStatus) == 'notConnected' and not board.ignitionLine and not board.ignitionCan
            for board in state
          )
        elif name == 'managerState':
          if not 0 < len(state.processes) <= 128:
            raise ValueError('source_size')
          value['processes'] = {str(proc.name): [proc.running, proc.shouldBeRunning] for proc in state.processes}
          if len(value['processes']) != len(state.processes):
            raise ValueError('duplicate_producer')
        elif name == 'carParams':
          value.update(vehicle=str(state.carFingerprint), notCar=state.notCar, dashcam=state.passive or state.dashcamOnly)
        elif name == 'carState':
          value.update(
            can_valid=state.canValid,
            can_timeout=state.canTimeout,
            standstill=state.standstill,
            gear=str(state.gearShifter),
            raw_speed=state.vEgoRaw,
            speed=state.vEgo,
            cruise=state.cruiseState.enabled,
            aeb=state.stockAeb,
          )
        elif name == 'carControl':
          value.update(enabled=state.enabled, lat=state.latActive, long=state.longActive)
        elif name == 'selfdriveState':
          value.update(enabled=state.enabled, active=state.active, state=str(state.state), critical=str(state.alertStatus) == 'critical')
        else:
          if len(state) > 64:
            raise ValueError('source_size')
          value['disable'] = any(row.immediateDisable or row.softDisable for row in state)
        self.samples[name] = value
    return self.samples


def protected(path, directory=False, private=False):
  info = path.lstat()
  if path.is_symlink() or info.st_uid != 0 or info.st_mode & (0o077 if private else 0o022):
    raise ValueError('protected_owner_path_required')
  if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
    raise ValueError('protected_owner_path_required')


def atomic(path, data, mode=0o600):
  temporary = path.with_name(path.name + '.new')
  if path.is_symlink() or temporary.is_symlink():
    raise ValueError('unsafe_destination')
  with temporary.open('wb') as stream:
    os.chmod(temporary, mode)
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())
  temporary.replace(path)


def prerequisites(check_binaries=False, check=lambda: None):
  if sys.platform != 'linux' or platform.machine() != 'aarch64' or os.geteuid() != 0 or not Path('/TICI').exists():
    raise ValueError('owner_comma_local_install_required')
  if Path('/proc/1/comm').read_text().strip() != 'systemd':
    raise ValueError('systemd_required')
  protected(ROOT, directory=True)
  protected(ROOT / 'state', directory=True)
  identities = {}
  for name, digest in BINARY_SHA.items():
    path = BIN / name
    protected(path)
    info = path.stat()
    identities[name] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    if check_binaries:
      value = hashlib.sha256()
      with path.open('rb') as stream:
        while block := stream.read(1048576):
          check()
          value.update(block)
      if value.hexdigest() != digest:
        raise ValueError('enrolled_binary_identity')
  if not Path('/usr/sbin/sshd').is_file() or not Path('/usr/bin/ssh-keygen').is_file():
    raise ValueError('existing_openssh_required')
  return identities


def run(args, timeout=5):
  result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=True)
  if len(result.stdout) > 16384:
    raise ValueError('command_result_budget')
  return result.stdout


def exchange(host, path='/v1/remote/bootstrap'):
  import http.client
  from urllib.parse import urlsplit

  source = Path('/data/pv5-incident-report.json')
  info = source.lstat()
  if source.is_symlink() or info.st_uid not in (0, 1000) or info.st_mode & 0o077 or not 0 < info.st_size <= 4096:
    raise ValueError('existing_enrollment_required')
  cfg = json.loads(source.read_bytes())
  target, key = urlsplit(cfg['endpoint']), bytes.fromhex(cfg['key'])
  if target.scheme != 'https' or not target.hostname.endswith('.ts.net') or target.port != 10000 or target.path != '/v1/incident' or len(key) != 32:
    raise ValueError('existing_enrollment_required')
  stamp, nonce = int(time.time()), os.urandom(16).hex()
  body = encode({'version': 1, 'host_key': host})
  message = f'pv5-archive-v1\n{stamp}\n{nonce}\n{path}\n'.encode() + hashlib.sha256(body).digest()
  values = {
    'X-PV5-Time': str(stamp),
    'X-PV5-Nonce': nonce,
    'X-PV5-Signature': hmac.new(key, message, hashlib.sha256).hexdigest(),
    'Content-Type': 'application/json',
  }
  connection = http.client.HTTPSConnection(target.hostname, target.port, timeout=8)
  try:
    connection.request('POST', path, body, values)
    response = connection.getresponse()
    data = response.read(4097)
    if response.status != 200 or len(data) > 4096:
      raise ValueError('pc_bootstrap_refused')
    result = json.loads(data)
    if result.get('version') != 1:
      raise ValueError('pc_bootstrap_refused')
    return result
  finally:
    connection.close()


def ssh_config():
  return '\n'.join(
    [
      'ListenAddress 127.0.0.1',
      'AddressFamily inet',
      'Port 2223',
      'PidFile ' + str(RUNTIME / 'sshd.pid'),
      'HostKey ' + str(ROOT / 'conditional_host_ed25519'),
      'AuthorizedKeysFile ' + str(RUNTIME / 'authorized_keys'),
      'StrictModes yes',
      'AllowUsers root',
      'PermitRootLogin prohibit-password',
      'PubkeyAuthentication yes',
      'PasswordAuthentication no',
      'KbdInteractiveAuthentication no',
      'AuthenticationMethods publickey',
      'UsePAM yes',  # Keep account policy; host logind/D-Bus is blocked by the service namespace.
      'AllowAgentForwarding no',
      'AllowTcpForwarding no',
      'X11Forwarding no',
      'PermitTunnel no',
      'PermitUserEnvironment no',
      'PermitUserRC no',
      'MaxSessions 2',
      'MaxAuthTries 2',
      'LoginGraceTime 15',
      'ClientAliveInterval 5',
      'ClientAliveCountMax 2',
      'LogLevel ERROR',
      '',
    ]
  )


def unit_text(interpreter, policy=HOME_POLICY):
  if policy not in (HOME_POLICY, PARK_POLICY):
    raise ValueError('invalid_resource_policy')
  return '\n'.join(
    [
      '[Unit]',
      'Description=Owner PV5 conditional dashcam diagnostics',
      'After=network.target',
      '[Service]',
      'Type=notify',
      'NotifyAccess=main',
      'WatchdogSec=1s',
      'WatchdogSignal=SIGKILL',
      'Restart=always',
      'RestartSec=5s',
      'KillMode=control-group',
      'TimeoutStopSec=200ms',
      'TimeoutStartSec=30s',
      *(['CPUAccounting=yes', 'CPUQuota=10%', 'MemoryAccounting=yes', 'MemoryMax=256M', 'TasksMax=64'] if policy == PARK_POLICY else []),
      'Nice=19',
      'IOSchedulingClass=idle',
      'LimitCORE=0',
      'LimitNOFILE=256',
      'RuntimeDirectory=pv5-conditional-remote',
      'RuntimeDirectoryMode=0755',
      'ProtectControlGroups=yes',
      'ProtectKernelTunables=yes',
      'ProtectKernelModules=yes',
      'NoNewPrivileges=yes',
      'RestrictSUIDSGID=yes',
      'PrivateTmp=yes',
      'TemporaryFileSystem=/run/sshd:ro',
      'InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private',
      'WorkingDirectory=/data/openpilot',
      'Environment=GOMAXPROCS=1',
      'Environment=PYTHONDONTWRITEBYTECODE=1',
      'Environment=PYTHONPATH=/data/openpilot/pydeps:/data/openpilot',
      'ExecStartPre=/usr/sbin/sshd -t -f ' + str(ROOT / 'conditional_sshd_config'),
      'ExecStart=' + interpreter + ' ' + str(SCRIPT) + ' --guard',
      '[Install]',
      'WantedBy=multi-user.target',
      '',
    ]
  )


def notify(text):
  address = os.environ.get('NOTIFY_SOCKET', '')
  if not address or os.environ.get('WATCHDOG_USEC') != '1000000':
    raise ValueError('pid1_watchdog_required')
  with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
    connection.settimeout(0.05)
    connection.sendto(text.encode(), ('\0' + address[1:]) if address.startswith('@') else address)


class Permission:
  def __init__(self):
    self.stamp, self.closed = None, False
    threading.Thread(target=self.watch, daemon=True, name='remote-permission').start()

  def check(self):
    if self.closed or self.stamp is None or not 0 <= time.monotonic() - self.stamp < 0.4:
      raise ValueError('permission_expired')

  def watch(self):
    buffer = b''
    began = time.monotonic()
    while True:
      ready, _, _ = select.select([0], [], [], 0.05)
      if ready:
        raw = os.read(0, 4096)
        if not raw or len(buffer) + len(raw) > 4096:
          break
        buffer += raw
        while b'\n' in buffer:
          line, buffer = buffer.split(b'\n', 1)
          try:
            stamp = float(line)
            if not 0 <= time.monotonic() - stamp < 0.4:
              raise ValueError()
            self.stamp = stamp
          except ValueError:
            self.closed = True
            break
      if self.closed or (time.monotonic() - began > 1 and (self.stamp is None or time.monotonic() - self.stamp >= 0.4)):
        break
    os._exit(75)  # PID1/parent observes exit and kills THIS unit's descendants.


def wait_backend(cli, permission, deadline):
  while time.monotonic() < deadline:
    permission.check()
    try:
      state = json.loads(run(cli + ['status', '--json', '--peers=false'], timeout=min(2, max(0.1, deadline - time.monotonic()))))
    except subprocess.CalledProcessError:
      state = {}  # The owned socket may exist before its API is ready.
    permission.check()
    if state.get('BackendState') == 'Running':
      return
    time.sleep(1)
  raise ValueError('node_not_running')  # No inferred login requirement or auth URL.


def runtime_key():
  mark('runtime_key')
  source = ROOT / 'conditional_authorized_keys'
  protected(source, private=True)
  if not 0 < source.stat().st_size <= 512:
    raise ValueError('authorized_key_format')
  for path in (RUNTIME, RUNTIME.parent, Path('/')):
    protected(path, directory=True)
  data = source.read_bytes()
  if not 0 < len(data) <= 512:
    raise ValueError('authorized_key_format')
  key = public_key(' '.join(data.decode().split()[-2:]))
  if data != ('no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ' + key + '\n').encode():
    raise ValueError('authorized_key_format')
  atomic(RUNTIME / 'authorized_keys', data)


def bridge():
  permission = Permission()
  began = time.monotonic()
  while permission.stamp is None and time.monotonic() - began < 0.8:
    time.sleep(0.02)
  permission.check()
  children = []
  try:
    runtime_key()
    permission.check()
    # This dedicated daemon is absent in normal/active driving mode.
    children.append(
      subprocess.Popen(
        [str(BIN / 'tailscaled'), '--tun=userspace-networking', '--statedir=' + str(ROOT / 'state'), '--socket=' + SOCKET, '--port=0'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
      )
    )
    while not Path(SOCKET).exists():
      permission.check()
      if children[0].poll() is not None or time.monotonic() - began > 15:
        raise ValueError('daemon_not_ready')
      time.sleep(0.05)
    cli = [str(BIN / 'tailscale'), '--socket=' + SOCKET]
    wait_backend(cli, permission, time.monotonic() + 15)
    serve = json.loads(run(cli + ['serve', 'status', '--json']))
    if serve and (
      set(serve) - {'TCP', 'AllowFunnel'} or serve.get('AllowFunnel') or serve.get('TCP') not in (None, {}, {'2222': {'TCPForward': '127.0.0.1:2223'}})
    ):
      raise ValueError('foreign_serve_configuration')
    permission.check()
    prior = ROOT / 'conditional_prior_prefs.json'
    if not prior.exists():
      prefs = json.loads(run(cli + ['debug', 'prefs']))
      atomic(prior, encode({'run_ssh': prefs.get('RunSSH') is True}))
    run(cli + ['set', '--ssh=false'])  # Replace repeated browser-check SSH with owner key SSH.
    permission.check()
    children.append(
      subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(ROOT / 'conditional_sshd_config')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    )
    run(cli + ['serve', '--bg', '--tcp=2222', 'tcp://127.0.0.1:2223'])
    while True:
      permission.check()
      if any(child.poll() is not None for child in children):
        raise ValueError('owned_daemon_exited')
      time.sleep(0.05)
  finally:
    for child in children:
      if child.poll() is None:
        child.terminate()


def guard():
  identities = prerequisites()
  protected(SCRIPT)
  protected(ROOT / 'conditional_modes.json', private=True)
  config = json.loads((ROOT / 'conditional_modes.json').read_bytes())
  if access_policy(config) == PARK_POLICY:
    resource_limits(config)
  modes = config['qualified_modes']
  if config.get('binaries') != identities:
    raise ValueError('enrolled_binary_metadata_changed')
  if config.get('trial') is not True and config.get('guard_sha256') != hashlib.sha256(SCRIPT.read_bytes().replace(b'\r\n', b'\n')).hexdigest():
    raise ValueError('matching_code_qualification_required')
  os.setpriority(os.PRIO_PROCESS, 0, 19)
  source, gate, child = Source(), Gate(), None
  foreign_checked = False

  def trial_exit(reason, code=None, gap=None):
    if config.get('trial') is True:
      print(json.dumps({'trial_exit': reason, 'bridge_exit_code': code, 'loop_gap_ms': gap}), flush=True)

  notify('READY=1\nWATCHDOG=1')
  try:
    while True:
      if config.get('trial') is True and (RUNTIME / 'stop-trial').exists():
        trial_exit('trial_requested_stop')
        return
      mode, _reason = eligible(source.read(), time.monotonic(), modes)
      if mode and not foreign_checked:
        for entry in Path('/proc').glob('[0-9]*'):
          try:
            if (entry / 'comm').read_text().strip() == 'tailscaled':
              raise ValueError('foreign_daemon_present')
          except FileNotFoundError:
            continue
        foreign_checked = True
        mode, _reason = eligible(source.read(), time.monotonic(), modes)
      now = time.monotonic()
      gap = round((now - gate.last) * 1000) if gate.last is not None else None
      action = gate.tick(mode, now)
      if action == 'close' or (child is not None and child.poll() is not None):
        trial_exit((_reason or 'loop_timing') if action == 'close' else 'bridge_exited', child.poll() if child is not None else None, gap)
        return  # PID1 closes the entire owned cgroup, then restarts the idle gate.
      if action == 'open':
        child = subprocess.Popen([sys.executable, str(SCRIPT), '--bridge'], stdin=subprocess.PIPE)
        os.set_blocking(child.stdin.fileno(), False)
      if child is not None:
        os.write(child.stdin.fileno(), f'{now:.9f}\n'.encode())
      notify('WATCHDOG=1')
      time.sleep(0.1 if child is not None else 0.5)
  finally:
    if child is not None:
      child.stdin.close()


def runtime_unit():
  """Register only in writable /run; never remount or write the AGNOS root."""
  legacy = Path('/etc/systemd/system') / UNIT
  if legacy.exists() or legacy.is_symlink():
    raise ValueError('persistent_unit_requires_manual_migration')
  protected(ROOT / UNIT)
  RUNTIME_UNITS.mkdir(parents=True, exist_ok=True)
  protected(RUNTIME_UNITS, directory=True)
  target = RUNTIME_UNITS / UNIT
  if target.exists() or target.is_symlink():
    if not target.is_symlink() or target.resolve() != ROOT / UNIT:
      raise ValueError('foreign_unit_not_replaced')
  else:
    target.symlink_to(ROOT / UNIT)
  return target


def require_boot_hook():
  path = Path('/data/openpilot/launch_chffrplus.sh')
  if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
    raise ValueError('launcher_update_required')
  if BOOT_HOOK.encode() not in path.read_bytes().replace(b'\r\n', b'\n'):
    raise ValueError('launcher_update_required')


def restore_runtime():
  """One bounded boot invocation; the PID1 guard itself admits all networking."""
  mark('boot_qualification')
  identities = prerequisites()
  protected(SCRIPT)
  modes_path = ROOT / 'conditional_modes.json'
  protected(modes_path, private=True)
  if modes_path.stat().st_size > 4096:
    raise ValueError('qualification_size')
  config = json.loads(modes_path.read_bytes())
  if config.get('trial') is not False or config.get('boot_restore') is not True:
    return {'boot_restore_requested': False, 'reason': 'not_enabled'}
  policy = access_policy(config)
  if config.get('binaries') != identities or config.get('guard_sha256') != hashlib.sha256(SCRIPT.read_bytes().replace(b'\r\n', b'\n')).hexdigest():
    raise ValueError('matching_code_qualification_required')
  protected(ROOT / UNIT)
  if (ROOT / UNIT).read_bytes() != unit_text(sys.executable, policy).encode():
    raise ValueError('qualified_runtime_interpreter_changed')
  trial = RUNTIME_UNITS / (UNIT + '.d/trial.conf')
  if trial.exists() or trial.is_symlink():
    raise ValueError('unexpected_trial_dropin')
  for old in ('pv5-home-remote-access.service', 'pv5-remote-access.service'):
    if run(['systemctl', 'show', old, '-p', 'ActiveState', '--value']).strip() in (b'active', b'activating', b'reloading', b'deactivating'):
      raise ValueError('existing_session_active_nothing_stopped')
  mark('runtime_unit_restore')
  target = runtime_unit()
  state = run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value']).strip()
  if state in (b'active', b'activating', b'reloading', b'deactivating'):
    fragment = run(['systemctl', 'show', UNIT, '-p', 'FragmentPath', '--value']).strip()
    if fragment not in (str(target).encode(), str(ROOT / UNIT).encode()):
      raise ValueError('active_unit_identity')
    return {'boot_restore_requested': False, 'already_running': True}
  run(['systemctl', 'daemon-reload'])
  run(['systemctl', 'start', '--no-block', UNIT])
  return {'boot_restore_requested': True, 'network_admitted': False}


def kernel_resource_support():
  """Read existing controllers before modifying identities or installing files."""
  root = Path('/sys/fs/cgroup')
  controllers = root / 'cgroup.controllers'
  available = controllers.read_text().split() if controllers.is_file() else []
  cpu = 'cpu' in available or any((root / name / 'cpu.cfs_quota_us').is_file() for name in ('cpu', 'cpu,cpuacct', 'cpuacct,cpu'))
  if not cpu:
    raise ValueError('vehicle_cpu_controller_unavailable')
  if 'memory' not in available:
    raise ValueError('vehicle_memory_controller_unavailable')
  if 'pids' not in available:
    raise ValueError('vehicle_tasks_controller_unavailable')


def await_eligible(source, modes, *, clock=time.monotonic, pause=time.sleep):
  # card publishes CarParams every 50s; a fresh subscriber may miss the prior event.
  deadline = clock() + (55 if modes == PARK_MODES else 5)
  gate = Gate()
  while clock() < deadline:
    samples = source.read()
    now = clock()
    mode, _ = eligible(samples, now, modes)
    if gate.tick(mode, now) == 'open':
      return mode
    pause(0.1)
  raise ValueError('eligible_local_state_required')


def install(parked=False):
  policy = PARK_POLICY if parked else HOME_POLICY
  modes = PARK_MODES if parked else ['detached_home']
  mark('prerequisites')
  prerequisites()
  # Only LOCAL installation. Do not replace the transport that is carrying us.
  for name in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'):
    if os.environ.get(name):
      raise ValueError('local_terminal_install_required')
  for old in ('pv5-home-remote-access.service', 'pv5-remote-access.service', UNIT):
    result = run(['systemctl', 'show', old, '-p', 'ActiveState', '--value']).strip()
    if result in (b'active', b'activating', b'reloading', b'deactivating'):
      raise ValueError('existing_session_active_nothing_stopped')
  if run(['systemctl', 'show', UNIT, '-p', 'UnitFileState', '--value']).strip() in (b'enabled', b'enabled-runtime'):
    raise ValueError('auto_service_already_enabled')
  existing = ROOT / 'conditional_modes.json'
  if existing.exists() or existing.is_symlink():
    protected(existing, private=True)
    if access_policy(json.loads(existing.read_bytes())) != policy:
      raise ValueError('other_mode_enrolled')
  if parked:
    mark('vehicle_resource_preflight')
    kernel_resource_support()
  mark('park_source' if parked else 'home_source')
  source = Source()
  admitted_mode = await_eligible(source, modes)

  def still_home():
    if eligible(source.read(), time.monotonic(), modes)[0] != admitted_mode:
      raise ValueError('home_condition_lost')

  mark('binary_identity')
  identities = prerequisites(check_binaries=True, check=still_home)
  host = ROOT / 'conditional_host_ed25519'
  if not host.exists():
    run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(host)])
  protected(host, private=True)
  host_public = public_key(' '.join(Path(str(host) + '.pub').read_text().split()[:2]))
  mark('pc_bootstrap')
  response = exchange(host_public)  # Existing private enrollment, no key through Discord.
  if response.get('host_key_pinned') is not True:
    raise ValueError('pc_host_pin_required')
  client = public_key(response['client_public_key'])
  if eligible(source.read(), time.monotonic(), modes)[0] != admitted_mode:
    raise ValueError('home_condition_lost')
  mark('data_install')
  atomic(ROOT / 'conditional_authorized_keys', ('no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ' + client + '\n').encode())
  atomic(ROOT / 'conditional_sshd_config', ssh_config().encode())
  atomic(SCRIPT, Path(__file__).read_bytes(), 0o644)
  atomic(ROOT / 'conditional_modes.json', encode({'qualified_modes': modes, 'resource_policy': policy, 'trial': True, 'binaries': identities}))
  unit = ROOT / UNIT
  atomic(unit, unit_text(sys.executable, policy).encode(), 0o644)
  mark('runtime_unit_link')
  runtime_unit()
  run(['systemctl', 'daemon-reload'])
  trial = RUNTIME_UNITS / (UNIT + '.d')
  trial.mkdir(exist_ok=True)
  atomic(trial / 'trial.conf', b'[Service]\nRuntimeMaxSec=900s\nRestart=no\n', 0o644)
  run(['systemctl', 'daemon-reload'])
  mark('trial_start')
  run(['systemctl', 'start', UNIT], timeout=35)
  print(
    json.dumps(
      {
        'prepared': True,
        'trial_started': True,
        'maximum_trial_seconds': 900,
        'qualified_mode': admitted_mode,
        'resource_policy': policy,
        'boot_enabled': False,
        'vehicle_dashcam_qualified': False,
        'key_auth_configured': True,
        'actual_PC_ssh_verified': False,
      }
    ),
    flush=True,
  )


def enable_home(parked=False):
  policy = PARK_POLICY if parked else HOME_POLICY
  modes = PARK_MODES if parked else ['detached_home']
  mark('home_enable')
  identities = prerequisites()
  if any(os.environ.get(name) for name in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY')):
    raise ValueError('local_terminal_enable_required')
  protected(SCRIPT)
  protected(ROOT / 'conditional_modes.json', private=True)
  prior_config = json.loads((ROOT / 'conditional_modes.json').read_bytes())
  if access_policy(prior_config) != policy:
    raise ValueError('matching_mode_trial_required')
  target = RUNTIME_UNITS / UNIT
  if not target.is_symlink() or target.resolve() != ROOT / UNIT:
    raise ValueError('unit_identity')
  require_boot_hook()
  if run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value']).strip() in (b'active', b'activating', b'deactivating'):
    raise ValueError('finish_trial_cancellation_check_first')
  source = Source()
  admitted_mode = await_eligible(source, modes)
  host = public_key(' '.join((ROOT / 'conditional_host_ed25519.pub').read_text().split()[:2]))
  proof = exchange(host, '/v1/remote/park-qualification' if parked else '/v1/remote/qualification')
  digest = hashlib.sha256(SCRIPT.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
  if proof.get('park_qualified' if parked else 'home_qualified') is not True or proof.get('guard_sha256') != digest or proof.get('resource_policy') != policy:
    raise ValueError('matching_actual_device_qualification_required')
  audit = ROOT / 'conditional_trial_processes.json'
  protected(audit, private=True)
  if audit.stat().st_size > 2048:
    raise ValueError('trial_pid_audit')
  rows = json.loads(audit.read_bytes())['processes']
  if not 2 <= len(rows) <= 8:
    raise ValueError('trial_pid_audit')
  for entry in rows:
    pid, start = entry['pid'], entry['start']
    if type(pid) is not int or not 1 < pid < 2**30:
      raise ValueError('trial_pid_audit')
    try:
      values = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
      if values[19] == start and values[0] != 'Z':
        raise ValueError('trial_process_survived')
    except FileNotFoundError:
      pass
  if eligible(source.read(), time.monotonic(), modes)[0] != admitted_mode:
    raise ValueError('home_condition_lost')
  trial = RUNTIME_UNITS / (UNIT + '.d/trial.conf')
  if trial.is_symlink() or trial.read_bytes() != b'[Service]\nRuntimeMaxSec=900s\nRestart=no\n':
    raise ValueError('trial_identity')
  protected(ROOT / 'conditional_modes.json', private=True)
  prior_modes = json.loads((ROOT / 'conditional_modes.json').read_bytes())
  if access_policy(prior_modes) != policy:
    raise ValueError('matching_mode_trial_required')
  if prior_modes.get('binaries') != identities:
    raise ValueError('enrolled_binary_metadata_changed')
  trial.unlink()
  atomic(
    ROOT / 'conditional_modes.json',
    encode({
      'qualified_modes': modes, 'resource_policy': policy, 'trial': False,
      'boot_restore': True, 'guard_sha256': digest, 'binaries': identities,
    }),
  )
  run(['systemctl', 'daemon-reload'])
  run(['systemctl', 'start', UNIT], timeout=35)
  print(
    json.dumps(
      {'home_auto_enabled': not parked, 'park_auto_enabled': parked, 'manual_expiry_removed': True, 'boot_enabled': True, 'local_trial_pid_audit_passed': True, 'vehicle_dashcam_qualified': False}
    ),
    flush=True,
  )


def main():
  parser = argparse.ArgumentParser()
  group = parser.add_mutually_exclusive_group(required=True)
  group.add_argument('--install-trial', action='store_true')
  group.add_argument('--install-park-trial', action='store_true')
  group.add_argument('--enable-park-auto', action='store_true')
  group.add_argument('--enable-home-auto', action='store_true')
  group.add_argument('--restore-runtime', action='store_true')
  group.add_argument('--guard', action='store_true')
  group.add_argument('--bridge', action='store_true')
  args = parser.parse_args()
  try:
    if args.install_trial or args.install_park_trial or args.enable_home_auto or args.enable_park_auto or args.restore_runtime:
      import fcntl

      lock_path = ROOT / 'conditional_install.lock'
      if lock_path.is_symlink():
        raise ValueError('unsafe_install_lock')
      with lock_path.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.restore_runtime:
          print(json.dumps(restore_runtime()), flush=True)
        else:
          install(args.install_park_trial) if (args.install_trial or args.install_park_trial) else enable_home(args.enable_park_auto)
    elif args.guard:
      guard()
    else:
      bridge()
  except Exception as error:
    reason = str(error) if re.fullmatch(r'[a-z_]{1,70}', str(error)) else type(error).__name__
    result = {'success': False, 'reason': reason, 'stage': STAGE}
    if isinstance(error, OSError) and type(error.errno) is int:
      result['errno'] = error.errno
    print(json.dumps(result), flush=True)
    return 1
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
