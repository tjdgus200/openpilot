"""Separate Park installation, node state, proof and boot lifecycle on a PC."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.pv5_remote import conditional_remote as remote
from tools.pv5_remote.test_remote import key, parked


class SidecarTests(unittest.TestCase):
  def setUp(self):
    stack = ExitStack()
    self.addCleanup(stack.close)
    base = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    self.home, self.park = base / 'enrolled', base / 'enrolled/park'
    self.units, self.runtime = base / 'units', base / 'run/pv5-park-remote'
    self.park.mkdir(parents=True)
    (self.home / 'state').mkdir()
    self.script = self.park / 'conditional_remote.py'
    for name, value in {'ENROLLED_ROOT': self.home, 'PARK_ROOT': self.park, 'ROOT': self.park, 'PARK_SIDECAR': True,
                        'UNIT': remote.PARK_UNIT, 'SCRIPT': self.script, 'RUNTIME': self.runtime,
                        'RUNTIME_UNITS': self.units}.items():
      stack.enter_context(patch.object(remote, name, value))
    self.identities = {'fixture': [7, 8, 9, 10]}
    guard = b'previously qualified Home code\n'
    self.home_data = {
      'conditional_remote.py': guard, 'conditional_host_ed25519.pub': (key(1) + '\n').encode(),
      'conditional_authorized_keys': remote.authorized_key_data(key(2)),
      'conditional_host_ed25519': b'test-only SSH host fixture', 'state/tailscaled.state': b'independent Home fixture',
      'conditional_modes.json': remote.encode({'qualified_modes': ['detached_home'], 'trial': False, 'boot_restore': True,
        'resource_policy': remote.HOME_POLICY, 'guard_sha256': hashlib.sha256(guard).hexdigest(), 'binaries': self.identities}),
    }
    for name, data in self.home_data.items():
      (self.home / name).write_bytes(data)
    self.state = 'inactive'
    self.lifecycle = []
    self.proof = {}
    self.cfg = self.park / 'conditional_modes.json'
    self.policy = remote.PARK_COMPAT_POLICY

    def protected(path, directory=False, private=False):
      if path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError('protected_owner_path_required')

    def service(args, **kwargs):
      if args[0] == '/usr/bin/ssh-keygen':
        self.assertEqual(args, ['/usr/bin/ssh-keygen', '-y', '-f', str(self.home / 'conditional_host_ed25519')])
        return key(1).encode()
      if args[1] == 'show':
        if args[4] == 'ActiveState':
          return (self.state if args[2] == remote.PARK_UNIT else 'inactive').encode()
        return b''
      self.lifecycle.append(args)
      return b''

    def exchange(host, path):
      self.assertEqual(host, key(1))
      if path == '/v1/remote/park-sidecar-bootstrap':
        return {'client_public_key': key(2), 'host_key_pinned': True}
      self.assertEqual(path, '/v1/remote/park-sidecar-qualification')
      return self.proof

    def samples():
      data = parked()
      for row in data.values():
        row['stamp'] = remote.time.monotonic()
      return data

    for name, obj in {'protected': protected, 'prerequisites': Mock(return_value=self.identities),
                      'run': service, 'exchange': exchange, 'Source': lambda: Mock(read=samples),
                      'await_eligible': Mock(return_value='parked_vehicle'), 'require_boot_hook': Mock(),
                      'kernel_resource_support': lambda: self.policy}.items():
      stack.enter_context(patch.object(remote, name, obj))
    stack.enter_context(patch.dict(os.environ, {}, clear=True))
    stack.enter_context(redirect_stdout(io.StringIO()))

  def assert_home_untouched(self):
    for name, data in self.home_data.items():
      self.assertEqual((self.home / name).read_bytes(), data, name)
    self.assertEqual({p.name for p in self.home.iterdir()}, {p.split('/')[0] for p in self.home_data} | {'park'})
    for args in self.lifecycle:
      self.assertTrue(args == ['systemctl', 'daemon-reload'] or
                      (args[1] == 'start' and args[-1] == remote.PARK_UNIT))

  def trial_evidence(self):
    remote.install(parked=True)
    cfg = json.loads(self.cfg.read_bytes())
    self.assertRegex(cfg['trial_id'], '^[a-f0-9]{32}$')
    self.assertTrue(cfg['park_sidecar'])
    self.assertTrue(cfg['trial'])
    self.assertFalse(remote.restore_runtime()['boot_restore_requested'])
    self.node = {'Self': {'ID': 'fixture-node', 'TailscaleIPs': ['100.100.0.2', 'fd00::2']}}
    remote.park_node(self.node, cfg)
    self.proof = {'park_qualified': True, 'guard_sha256': cfg['guard_sha256'], 'resource_policy': self.policy,
                  'trial_id': cfg['trial_id'], 'target_ip': '100.100.0.2'}
    audit = {'trial_id': cfg['trial_id'], 'processes': [{'pid': 103000001, 'start': '4'}, {'pid': 103000002, 'start': '5'}]}
    (self.park / 'conditional_trial_processes.json').write_bytes(remote.encode(audit))
    return cfg

  def test_separate_trial_enable_boot_and_permanent_reinstall_refusal(self):
    self.trial_evidence()
    remote.enable_home(parked=True)
    cfg = json.loads(self.cfg.read_bytes())
    self.assertEqual(cfg['park_node'], {'id': 'fixture-node', 'ip': '100.100.0.2'})
    self.assertTrue(cfg['park_sidecar'] and cfg['boot_restore'] and not cfg['trial'])
    self.assertFalse((self.park / 'conditional_host_ed25519').exists())
    sshd = (self.park / 'conditional_sshd_config').read_text()
    self.assertIn('Port 2224\n', sshd)
    self.assertIn('HostKey ' + str(self.home / 'conditional_host_ed25519'), sshd)
    self.assertIn('AuthorizedKeysFile ' + str(self.runtime / 'authorized_keys'), sshd)
    self.assertTrue(remote.restore_runtime()['boot_restore_requested'])
    with self.assertRaisesRegex(ValueError, 'already_enabled'):
      remote.install(parked=True)
    self.assertEqual(cfg, json.loads(self.cfg.read_bytes()))
    self.assert_home_untouched()

  def test_strict_sidecar_rejects_stale_trial_and_wrong_target(self):
    self.policy = remote.PARK_POLICY
    cfg = self.trial_evidence()
    for name, value in (('trial_id', 'f' * 32), ('target_ip', '100.100.0.3'), ('guard_sha256', '0' * 64)):
      with self.subTest(name=name), patch.dict(self.proof, {name: value}), self.assertRaises(ValueError):
        remote.enable_home(parked=True)
      self.assertEqual(cfg, json.loads(self.cfg.read_bytes()))
    remote.enable_home(parked=True)
    self.assertTrue(remote.restore_runtime()['boot_restore_requested'])
    self.assert_home_untouched()

  def test_prepopulated_park_state_refused(self):
    state = self.park / 'state'
    state.mkdir()
    (state / 'tailscaled.state').write_bytes(b'unowned fixture')
    with self.assertRaisesRegex(ValueError, 'empty_park_state_required'):
      remote.install(parked=True)
    self.assertFalse(self.cfg.exists())
    self.assertEqual((state / 'tailscaled.state').read_bytes(), b'unowned fixture')
    self.assert_home_untouched()

  def test_layout_and_node_changes_rejected(self):
    cfg = self.trial_evidence()
    with self.assertRaises(ValueError):
      remote.installed_policy({**cfg, 'park_sidecar': 1})
    with patch.object(remote, 'PARK_SIDECAR', False), self.assertRaises(ValueError):
      remote.installed_policy({**cfg, 'resource_policy': remote.PARK_POLICY})
    with self.assertRaisesRegex(ValueError, 'park_node_identity_changed'):
      remote.park_node({'Self': {'ID': 'different-node', 'TailscaleIPs': ['100.100.0.2']}}, cfg)
    self.assert_home_untouched()

  def test_unqualified_home_fails_before_writing_or_creating_node(self):
    (self.home / 'conditional_remote.py').write_bytes(b'new unqualified home source\n')
    with self.assertRaisesRegex(ValueError, 'qualified_home_identity_required'):
      remote.install(parked=True)
    self.assertEqual(list(self.park.iterdir()), [])
    self.assertEqual(self.lifecycle, [])

  def test_foreign_unit_source_and_owner_key_changes_leave_both_installations_intact(self):
    directory = self.units / (remote.PARK_UNIT + '.d')
    directory.mkdir(parents=True)
    extra = directory / 'override.conf'
    extra.write_text('[Service]\n')
    with self.assertRaisesRegex(ValueError, 'unexpected_trial_dropin'):
      remote.install(parked=True)
    self.assertEqual(list(self.park.iterdir()), [])
    extra.unlink()
    response = {'host_key_pinned': True, 'client_public_key': key(3)}
    with patch.object(remote, 'exchange', return_value=response), self.assertRaisesRegex(ValueError, 'qualified_home_identity_required'):
      remote.install(parked=True)
    self.assert_home_untouched()

  @unittest.skipUnless(sys.platform == 'linux', 'Linux socket owner and procfs layout')
  def test_foreign_socket_process_and_loopback_listener_refused(self):
    with patch.object(remote.socket, 'socket') as sock:
      client = sock.return_value.__enter__.return_value
      for pid, uid in ((123, 0), (567, 1000)):
        client.getsockopt.return_value = struct.pack('3i', pid, uid, 0)
        with self.assertRaisesRegex(ValueError, 'foreign_daemon_socket'):
          remote.owned_daemon_socket(567)
      client.getsockopt.return_value = struct.pack('3i', 567, 0, 0)
      remote.owned_daemon_socket(567)
    proc = self.park / 'proc'
    (proc / 'net').mkdir(parents=True)
    fds = proc / '567/fd'
    fds.mkdir(parents=True)
    (fds / '3').symlink_to('socket:[234]')
    tcp = proc / 'net/tcp'
    for addr, inode in (('00000000:08B0', 234), ('0100007F:08AF', 234), ('0100007F:08B0', 321)):
      tcp.write_text(f'header\n0: {addr} remote 0A x x x x x {inode}\n')
      self.assertFalse(remote.listener_owned(567, 2224, proc))
    tcp.write_text('header\n0: 0100007F:08B0 remote 0A x x x x x 234\n')
    self.assertTrue(remote.listener_owned(567, 2224, proc))
