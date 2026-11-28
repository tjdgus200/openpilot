"""Simulated owner PC, private receipt store and distinct Park service."""

import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpilot.system.pv5_incidents.archive_protocol import encode, headers
from tools.pv5_remote import client, connect, pc
from tools.pv5_remote.test_remote import key


class SidecarChecks(unittest.TestCase):
  def setUp(self):
    volume = tempfile.TemporaryDirectory()
    self.addCleanup(volume.cleanup)
    self.folder = Path(volume.name)
    for filename, value in [('client_ed25519', 'fake-key'), ('client_ed25519.pub', key(1)),
                            ('host_key.pub', key(2)), ('known_hosts', 'pv5-conditional-owner ' + key(2) + '\n')]:
      (self.folder / filename).write_text(value, encoding='ascii')
    release = Path(connect.__file__).resolve().parents[2]
    self.cfg = {'pc_enabled': True, 'park_code_root': str(release), 'ssh_executable': 'fixture-ssh',
                'private_directory': str(self.folder), 'device_ip': '100.64.0.1', 'park_device_ip': '100.64.0.2'}
    self.policy = {'conditional_remote': self.cfg}
    self.policy_path = self.folder / 'policy.json'
    self.policy_path.write_text(json.dumps(self.policy))
    guard = (release / 'tools/pv5_remote/conditional_remote.py').read_bytes()
    self.proof = {
      'resource_policy': connect.SIDECAR_POLICY, 'qualified_modes': ['parked_vehicle'], 'admitted_mode': 'parked_vehicle',
      'root_key_session': True, 'guard_cgroup': True, 'host_service_isolation': True, 'memory_limit': 268435456,
      'observed_memory_bytes': 12345678, 'CPU_controller': None, 'CPU_quota_limit': None, 'tasks_limit': None,
      'nice': 19, 'oom_score_adj': 500, 'cpu_affinity': [0, 1], 'nofile_limit': 256, 'core_limit': 0, 'no_new_privs': True,
      'trial': True, 'trial_id': '1' * 32, 'target_ip': self.cfg['park_device_ip'],
      'client_key_sha256': hashlib.sha256(key(1).encode()).hexdigest(),
      'guard_sha256': hashlib.sha256(guard.replace(b'\r\n', b'\n')).hexdigest(),
    }
    self.secret = bytes(range(32))
    self.bootstrap = pc.Bootstrap(self.folder, self.secret, lambda: True)

  def ingest(self, path, host=None):
    now = time.time()
    data = encode({'version': 1, 'host_key': key(2) if host is None else host})
    return self.bootstrap.ingest(path, headers(self.secret, path, data, now=now), data, now)

  def test_separate_address_required_and_home_pin_retained(self):
    args = connect.command(self.policy, sidecar=True)
    self.assertEqual(args[-1], 'root@100.64.0.2')
    self.assertIn('HostKeyAlias=pv5-conditional-owner', args)
    self.assertIn('IdentityAgent=none', args)
    self.assertEqual(connect.command(self.policy)[-1], 'root@100.64.0.1')
    for invalid in [None, '100.64.0.1', '127.0.0.1', '::1']:
      self.cfg['park_device_ip'] = invalid
      with self.assertRaisesRegex(ValueError, 'separate_enrolled_park_target_required'):
        connect.command(self.policy, sidecar=True)
    with self.assertRaisesRegex(ValueError, 'host_key_changed'):
      self.ingest(pc.SIDECAR_BOOTSTRAP, key(3))
    self.assertEqual(self.ingest(pc.SIDECAR_BOOTSTRAP)['client_public_key'], key(1))
    for name in ['known_hosts', 'host_key.pub']:
      (self.folder / name).unlink()
      with self.assertRaisesRegex(ValueError, 'host_not_pinned'):
        self.ingest(pc.SIDECAR_BOOTSTRAP)
      self.assertFalse((self.folder / name).exists())

  def test_proof_checks_profile_target_key_and_trial(self):
    _, args = connect.sidecar_probe_arguments(self.policy, True)
    self.assertTrue(connect.valid_sidecar_proof(self.proof, **args))
    strict = dict(self.proof, resource_policy=connect.PARK_POLICY, CPU_controller='v1', CPU_quota_limit=.1, tasks_limit=64)
    self.assertTrue(connect.valid_sidecar_proof(strict, **args))
    for fields in [{'CPU_quota_limit': .1}, {'nice': 0}, {'cpu_affinity': [True]}, {'no_new_privs': False},
                   {'target_ip': self.cfg['device_ip']}, {'guard_sha256': '0' * 64}, {'client_key_sha256': '0' * 64},
                   {'resource_policy': connect.HOME_POLICY}, {'trial_id': ''}, {'trial': False}, {'admitted_mode': ''}]:
      self.assertFalse(connect.valid_sidecar_proof(dict(self.proof, **fields), **args), fields)
    normal_probe, _ = connect.sidecar_probe_arguments(self.policy, False)
    self.assertIn('registration=False', normal_probe)
    compile(normal_probe, '<park-check>', 'exec')
    for template in [connect.JOB, connect.STOP]:
      code = connect.trial_command(template, sidecar=True, trial_id=self.proof['trial_id'], policy=connect.SIDECAR_POLICY)
      self.assertIn('/pv5-park-remote.service', code)
      self.assertIn("P('/data/pv5-remote-access/park')", code)
      self.assertIn(self.proof['trial_id'], code)
      compile(code, '<park-trial>', 'exec')

  def test_actual_sidecar_probe_reads_own_node_active_key_and_live_park(self):
    park = self.folder / 'guard'
    runtime = self.folder / 'runtime'
    cgroup = self.folder / 'cgroups/system.slice/pv5-park-remote.service'
    park.mkdir()
    runtime.mkdir()
    cgroup.mkdir(parents=True)
    group = self.folder / 'cgroup'
    group.write_text('0::/system.slice/pv5-park-remote.service')
    (cgroup / 'memory.current').write_text('12345678')
    (park / 'conditional_remote.py').write_bytes(b'fixture guard code')
    (park / 'conditional_modes.json').write_bytes(encode({k: self.proof[k] for k in ['qualified_modes', 'resource_policy', 'trial', 'trial_id']}))
    auth = runtime / 'authorized_keys'
    auth.write_text('no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ' + key(1) + '\n')
    expected = hashlib.sha256(b'fixture guard code').hexdigest()
    probe = connect.park_sidecar_check(expected, self.proof['client_key_sha256'], self.cfg['park_device_ip'], registration=True)
    for old, path in [('/proc/self/cgroup', group), ('/sys/fs/cgroup', self.folder / 'cgroups'),
                      ('/data/pv5-remote-access/park', park), ('/run/pv5-park-remote', runtime)]:
      probe = probe.replace(old, path.as_posix())
    runtime_module = {'resource_limits': lambda config: {'resource_policy': config['resource_policy']},
                      'public_key': pc.public_key, 'BIN': park, 'SOCKET': runtime / 'tailscaled.sock',
                      'Source': lambda: SimpleNamespace(read=lambda: {}), 'eligible': lambda *a, **kw: ('parked_vehicle', '')}
    with (patch.object(os, 'geteuid', return_value=0, create=True),
          patch('os.open', side_effect=OSError(errno.EROFS, 'fixture')),
          patch('socket.AF_UNIX', 1, create=True), patch('socket.socket') as net,
          patch('runpy.run_path', return_value=runtime_module), patch('time.sleep'), patch.object(sys, 'path', list(sys.path)),
          contextlib.redirect_stdout(io.StringIO()) as stdout):
      net.return_value.connect.side_effect = OSError(errno.ECONNREFUSED, 'fixture')
      def enrolled_node(args):
        self.assertIn('--socket=' + str(runtime_module['SOCKET']), args)
        return self.cfg['park_device_ip'].encode()
      runtime_module['run'] = enrolled_node
      runtime_module['await_eligible'] = lambda source, modes, *, registration: self.assertIs(registration, True)
      exec(compile(probe, '<sidecar-probe-fixture>', 'exec'), {})
      self.assertEqual(json.loads(stdout.getvalue())['trial_id'], self.proof['trial_id'])
      runtime_module['run'] = lambda args: self.cfg['device_ip'].encode()
      with self.assertRaisesRegex(AssertionError, 'unexpected_overlay_identity'):
        exec(compile(probe, '<sidecar-probe-fixture>', 'exec'), {})
      runtime_module['run'] = enrolled_node
      runtime_module['eligible'] = lambda *args, **kwargs: ('', 'stale')
      with self.assertRaisesRegex(AssertionError, 'actual_park_proof_required'):
        exec(compile(probe, '<sidecar-probe-fixture>', 'exec'), {})
      auth.write_text(auth.read_text().replace(key(1), key(3)))
      with self.assertRaisesRegex(AssertionError, 'enrolled_owner_key_changed'):
        exec(compile(probe, '<sidecar-probe-fixture>', 'exec'), {})

  def run_verify(self, refusal=b'connection refused', receipt=True):
    ops = []

    def ssh(args, **_):
      ops.append(args)
      self.assertEqual(args[-2], 'root@100.64.0.2')
      if len(ops) == 1:
        self.assertIn('registration=True', args[-1])
        return SimpleNamespace(returncode=0, stdout=encode(self.proof))
      if len(ops) == 2:
        self.assertIn('stop-trial', args[-1])
        return SimpleNamespace(returncode=0, stdout=b'{"stop_requested": true}' if receipt else b'')
      self.assertEqual(args[-1], 'true')
      if refusal is None:
        raise subprocess.TimeoutExpired('fixture-ssh', 8)
      return SimpleNamespace(returncode=255 if refusal else 0, stderr=refusal)

    job = SimpleNamespace(stdout=io.BytesIO(b'{"trial_job_ready":true}\n'), wait=lambda timeout: 255, poll=lambda: 255)
    with (patch.object(sys, 'argv', ['connect.py', '--policy', str(self.policy_path), '--verify-park-sidecar']),
          patch.object(subprocess, 'run', side_effect=ssh), patch.object(subprocess, 'Popen', return_value=job),
          contextlib.redirect_stdout(io.StringIO())):
      connect.main()

  def test_sidecar_receipt_only_after_cancel_and_reconnect_refusal(self):
    for reason, receipt in [(b'', True), (b'Host key verification failed', True), (None, True), (b'connection refused', False)]:
      with self.assertRaises(ValueError):
        self.run_verify(reason, receipt)
      self.assertFalse((self.folder / 'park-sidecar-qualification.json').exists())
    self.run_verify()
    result = self.ingest(pc.SIDECAR_QUALIFICATION)
    self.assertEqual(result['trial_id'], self.proof['trial_id'])
    self.assertEqual(result['target_ip'], self.cfg['park_device_ip'])
    with self.assertRaises(ValueError):
      self.ingest(pc.PARK_QUALIFICATION)
    with self.assertRaises(ValueError):
      self.ingest(pc.QUALIFICATION)

  def test_home_legacy_park_expired_or_changed_receipt_refused(self):
    path = self.folder / 'park-sidecar-qualification.json'
    receipt = dict(self.proof, passed=True, transport_closed=True, host_key=key(2),
                   local_pid_audit_pending=True, stamp=time.time())
    for name in ['home-qualification.json', 'park-qualification.json']:
      (self.folder / name).write_bytes(encode(receipt))
    with self.assertRaises(ValueError):
      self.ingest(pc.SIDECAR_QUALIFICATION)
    for fields in [{'stamp': time.time() - 1801}, {'trial_id': 'invalid'}, {'host_key': key(1)}, {'transport_closed': False},
                   {'guard_sha256': '2' * 64}, {'client_key_sha256': '3' * 64}, {'local_pid_audit_pending': False}]:
      path.write_bytes(encode(dict(receipt, **fields)))
      with self.assertRaises(ValueError):
        self.ingest(pc.SIDECAR_QUALIFICATION)

  def test_readonly_client_never_requests_registration_memory_exception(self):
    def ssh(args, **_):
      self.assertIn('registration=False', args[-1])
      self.assertEqual(args[-2], 'root@100.64.0.2')
      return SimpleNamespace(returncode=0, stdout=encode(self.proof))
    self.assertTrue(client.check(self.policy, runner=ssh, sidecar=True)['connected'])


if __name__ == '__main__':
  unittest.main()
