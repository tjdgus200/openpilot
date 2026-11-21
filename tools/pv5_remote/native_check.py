"""PC-only real SSH/PAM/cgroup test. No tailnet, device, or existing service use."""

import json
import os
import signal
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from tools.pv5_remote.connect import CHECK, CHECK_PARK
from tools.pv5_remote import conditional_remote as owner


def run(args, **kwargs):
  result = subprocess.run(args, capture_output=True, timeout=10, **kwargs)
  if result.returncode:
    sys.stderr.write(result.stderr.decode(errors='replace')[:1400])
    raise AssertionError('native_fixture_command_failed')
  return result.stdout


def main():
  assert os.geteuid() == 0 and not Path('/TICI').exists()
  binary = Path(os.environ['PV5_TEST_SSHD']).resolve()
  assert binary.is_file() and 'pc-tools/root/usr/sbin/sshd' in str(binary)
  token = 'pv5-native-ssh-' + uuid.uuid4().hex[:10]
  with tempfile.TemporaryDirectory(prefix=token + '-', dir='/run') as directory:
    root = Path(directory)
    assert root.resolve().parent == Path('/run')
    os.chmod(root, 0o755)
    for name in ('host', 'client', 'wrong'):
      run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(root / name)])
    (root / 'authorized').write_bytes((root / 'client.pub').read_bytes())
    (root / 'passwd').write_text('root:x:0:0:root:/root:/bin/bash\nsshd:x:65534:65534:SSH isolation:/run/sshd:/usr/sbin/nologin\n')
    strict_parent = '--strict-parent' in sys.argv
    parked = '--park-capped' in sys.argv
    auth_path, host_path = root / 'authorized', root / 'host'
    if strict_parent:
      data, safe = root / 'data', root / 'runtime'
      data.mkdir(mode=0o755)
      os.chown(data, 1000, 1000)  # This newly created PC fixture directory only.
      store = data / 'store'
      store.mkdir(mode=0o755)
      safe.mkdir(mode=0o755)
      auth_path, host_path = store / 'conditional_authorized_keys', store / 'host'
      client_public = ' '.join((root / 'client.pub').read_text().split()[:2])
      auth_path.write_text('no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ' + client_public + '\n')
      auth_path.chmod(0o600)
      host_path.write_bytes((root / 'host').read_bytes())
      host_path.chmod(0o600)
      (root / 'shadow').write_text('root:!:1:0:99999:7:::\nsshd:!:1:0:99999:7:::\n')
      (root / 'shadow').chmod(0o600)
    with socket.socket() as reservation:
      reservation.bind(('127.0.0.1', 0))
      port = reservation.getsockname()[1]
    config = '\n'.join(
      [
        'Port ' + str(port),
        'ListenAddress 127.0.0.1',
        'HostKey ' + str(host_path),
        'AuthorizedKeysFile ' + str(auth_path),
        'StrictModes yes',
        'PidFile ' + str(root / 'sshd.pid'),
        'AllowUsers root',
        'PermitRootLogin prohibit-password',
        'PubkeyAuthentication yes',
        'PasswordAuthentication no',
        'KbdInteractiveAuthentication no',
        'AuthenticationMethods publickey',
        'UsePAM yes',
        'AllowTcpForwarding no',
        'AllowAgentForwarding no',
        'PermitUserRC no',
        'LogLevel ERROR',
        '',
      ]
    )
    (root / 'sshd_config').write_text(config)
    if strict_parent:
      (root / 'missing_key_config').write_text(config.replace('AuthorizedKeysFile ' + str(auth_path), 'AuthorizedKeysFile ' + str(root / 'not_created_yet')))
    (root / 'known_hosts').write_text('pv5-native-test ' + (root / 'host.pub').read_text())
    (root / 'modes.json').write_text(json.dumps({'qualified_modes': owner.PARK_MODES if parked else ['detached_home'], 'resource_policy': owner.PARK_POLICY if parked else owner.HOME_POLICY, 'trial': True}))
    guard_source = Path(__file__).with_name('conditional_remote.py').read_text()
    if parked:
      # Synthetic parked input ONLY inside this isolated PC resource/SSH fixture.
      from tools.pv5_remote.test_remote import parked as parked_fixture
      guard_source += '\nUNIT = ' + repr(token + '.service') + '\n'
      guard_source += 'class Source:\n def read(self):\n  s=' + repr(parked_fixture()) + '\n  for v in s.values(): v["stamp"]=time.monotonic()\n  return s\n'
    (root / 'guard.py').write_text(guard_source)
    helper = root / 'helper.py'
    helper.write_text('''import os,socket,subprocess,time
from pathlib import Path
r=Path(__file__).parent
if (r/'missing_key_config').exists():
 subprocess.run([os.environ['PV5_TEST_SSHD'],'-t','-f',str(r/'missing_key_config')],check=True)
subprocess.run([os.environ['PV5_TEST_SSHD'],'-t','-f',str(r/'sshd_config')],check=True)
c=subprocess.Popen([os.environ['PV5_TEST_SSHD'],'-D','-e','-f',str(r/'sshd_config')],stdout=subprocess.DEVNULL,stderr=open(r/'sshd-error','wb'))
def notify(text):
 s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.connect(os.environ['NOTIFY_SOCKET']);s.send(text);s.close()
notify(b'READY=1\\nWATCHDOG=1')
while not (r/'deny').exists():
 if c.poll() is not None: raise SystemExit(1)
 notify(b'WATCHDOG=1');time.sleep(.05)
''')
    command = ['systemd-run', '--quiet', '--collect', '--unit=' + token, '--service-type=notify']
    for setting in (
      'NotifyAccess=main',
      'WatchdogSec=1s',
      'WatchdogSignal=SIGKILL',
      'KillMode=control-group',
      'TimeoutStopSec=200ms',
      'Restart=no',
      'Nice=19',
      'IOSchedulingClass=idle',
      'ProtectControlGroups=yes',
      'ProtectKernelTunables=yes',
      'ProtectKernelModules=yes',
      'NoNewPrivileges=yes',
      'RestrictSUIDSGID=yes',
      'PrivateTmp=yes',
      'InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private',
      'BindReadOnlyPaths=' + str(root / 'passwd') + ':/etc/passwd' + (' ' + str(root / 'shadow') + ':/etc/shadow' if strict_parent else ''),
      'TemporaryFileSystem=/run/sshd:ro',
    ):
      command.append('--property=' + setting)
    if parked:
      command += ['--property=CPUQuota=10%', '--property=MemoryMax=256M', '--property=TasksMax=64']
    command += ['--setenv=PV5_TEST_SSHD=' + str(binary), sys.executable, str(helper)]
    ssh = [
      'ssh',
      '-F',
      'none',
      '-o',
      'BatchMode=yes',
      '-o',
      'IdentitiesOnly=yes',
      '-o',
      'PasswordAuthentication=no',
      '-o',
      'KbdInteractiveAuthentication=no',
      '-o',
      'StrictHostKeyChecking=yes',
      '-o',
      'GlobalKnownHostsFile=/dev/null',
      '-o',
      'UserKnownHostsFile=' + str(root / 'known_hosts'),
      '-o',
      'HostKeyAlias=pv5-native-test',
      '-o',
      'ConnectTimeout=3',
      '-p',
      str(port),
    ]
    client = None
    try:
      run(command)
      for _ in range(40):
        try:
          with socket.create_connection(('127.0.0.1', port), timeout=0.1):
            break
        except OSError:
          time.sleep(0.1)
      if strict_parent:
        rejected = subprocess.run(ssh + ['-i', str(root / 'client'), 'root@127.0.0.1', 'true'], capture_output=True, timeout=5)
        assert rejected.returncode == 255 and b'permission denied' in rejected.stderr.lower(), 'uid1000_parent_not_rejected'
        from unittest.mock import patch

        with patch.object(owner, 'ROOT', store), patch.object(owner, 'RUNTIME', safe):
          owner.runtime_key()
        (root / 'sshd_config').write_text(config.replace('AuthorizedKeysFile ' + str(auth_path), 'AuthorizedKeysFile ' + str(safe / 'authorized_keys')))
        identity = run(['systemctl', 'show', token, '-p', 'ExecStart', '--value']).decode()
        assert str(helper) in identity
        run(['systemctl', 'restart', token])  # This exact PC-owned fixture only.
        for _ in range(40):
          try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.1):
              break
          except OSError:
            time.sleep(0.1)
      wrong = subprocess.run(ssh + ['-i', str(root / 'wrong'), 'root@127.0.0.1', 'true'], capture_output=True, timeout=5)
      assert wrong.returncode != 0, 'unregistered_key_accepted'
      check = (CHECK_PARK if parked else CHECK).replace('/pv5-conditional-remote.service', '/' + token + '.service')
      check = check.replace('/data/pv5-remote-access/conditional_modes.json', str(root / 'modes.json'))
      check = check.replace('/data/pv5-remote-access/conditional_remote.py', str(root / 'guard.py'))
      validated = json.loads(run(ssh + ['-i', str(root / 'client'), 'root@127.0.0.1', shlex.quote(sys.executable) + ' -c ' + shlex.quote(check)]))
      assert validated['host_service_isolation'] and validated['guard_cgroup']
      code = '; '.join(
        [
          'import json,os,pathlib,subprocess,time',
          "p=pathlib.Path('/proc/self/cgroup')",
          "c=subprocess.Popen(['/bin/sleep','60'])",
          "print(json.dumps({'group':p.read_text(),'pids':[os.getpid(),c.pid]}),flush=True)",
          'time.sleep(60)',
        ]
      )
      remote = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
      client = subprocess.Popen(ssh + ['-i', str(root / 'client'), 'root@127.0.0.1', remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      import select

      assert select.select([client.stdout], [], [], 6)[0], 'registered_key_auth_did_not_complete'
      raw = client.stdout.readline()
      if not raw:
        raise AssertionError('registered_key_rejected:' + client.stderr.read(500).decode(errors='replace'))
      proof = json.loads(raw)
      assert token in proof['group'], 'PAM_session_escaped_cgroup'
      began = time.monotonic()
      scenario = 'watchdog' if '--watchdog' in sys.argv else 'admission_loss'
      if scenario == 'watchdog':
        main_pid = int(run(['systemctl', 'show', token, '-p', 'MainPID', '--value']))
        assert str(helper).encode() in Path('/proc', str(main_pid), 'cmdline').read_bytes()
        os.kill(main_pid, signal.SIGSTOP)
      else:
        (root / 'deny').touch()
      client.wait(timeout=4)
      for _ in range(40):
        alive = [
          pid
          for pid in proof['pids']
          if Path('/proc', str(pid), 'stat').exists() and Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
        ]
        if not alive:
          break
        time.sleep(0.025)
      assert not alive, 'owned_remote_job_survived'
      print(
        json.dumps(
          {
            'scenario': scenario,
            'native_key_auth': True,
            'wrong_key_rejected': True,
            'PAM_session_in_guard_cgroup': True,
            'remote_job_and_child_terminated': True,
            'cancel_seconds_PC': round(time.monotonic() - began, 3),
            'device_validation': False,
            'uid1000_parent_rejection_then_runtime_key_success': strict_parent,
            'locked_root_PAM_fixture': strict_parent,
            'StrictModes_preserved': True,
            'missing_runtime_key_ExecStartPre_syntax_passed': strict_parent,
            'resource_policy': validated['resource_policy'],
            'service_memory_cap': validated['memory_limit'],
            'service_CPU_quota': validated['CPU_quota_limit'],
          }
        )
      )
    except Exception:
      if (root / 'sshd-error').exists():
        # Fixture server diagnostics only; no production credentials or logs.
        sys.stderr.write((root / 'sshd-error').read_text(errors='replace')[:2000])
      raise
    finally:
      identity = run(['systemctl', 'show', token, '-p', 'ExecStart', '--value']).decode()
      if str(helper) in identity:
        subprocess.run(['systemctl', 'stop', token], capture_output=True, timeout=5)
      if client is not None and client.poll() is None:
        client.kill()
        client.wait()


if __name__ == '__main__':
  main()
