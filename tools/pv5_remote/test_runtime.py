"""Writable runtime registration and qualification-preserving boot restore."""

import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.pv5_remote import conditional_remote as remote
from tools.pv5_remote.test_remote import key


class RuntimeChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.base = Path(self.temp.name)
    self.root, self.units = self.base / 'data', self.base / 'run/systemd/system'
    self.root.mkdir()
    self.script = self.root / 'conditional_remote.py'
    self.script.write_bytes(b'qualified public fixture script\n')
    self.config = self.root / 'conditional_modes.json'
    self.identities = {'fixture': [1, 2, 3, 4]}
    self.calls = []
    self.state = b'inactive'
    self.unit = 'pv5-runtime-fixture.service'
    self.stack = contextlib.ExitStack()
    for name, value in [('ROOT', self.root), ('SCRIPT', self.script), ('UNIT', self.unit), ('RUNTIME_UNITS', self.units)]:
      self.stack.enter_context(patch.object(remote, name, value))
    self.stack.enter_context(patch.object(remote, 'prerequisites', return_value=self.identities))
    self.stack.enter_context(patch.object(remote, 'run', side_effect=self.query))
    # Ownership/mount enforcement is exercised by the native read-only test;
    # these temporary fixtures also run on Windows, where uid/mode differ.
    self.stack.enter_context(patch.object(remote, 'protected', side_effect=self.owned_path))
    self.addCleanup(self.stack.close)
    self.addCleanup(self.temp.cleanup)
    (self.root / self.unit).write_bytes(remote.unit_text(sys.executable).encode())
    self.write_config()

  def owned_path(self, path, directory=False, private=False):
    path = Path(path)
    self.assertTrue(path.is_relative_to(self.base))
    self.assertFalse(path.is_symlink())
    self.assertTrue(path.is_dir() if directory else path.is_file())

  def write_config(self, **changes):
    value = {
      'qualified_modes': ['detached_home'],
      'resource_policy': remote.HOME_POLICY,
      'trial': False,
      'boot_restore': True,
      'guard_sha256': hashlib.sha256(self.script.read_bytes()).hexdigest(),
      'binaries': self.identities,
    }
    value.update(changes)
    self.config.write_text(json.dumps(value), encoding='utf-8')
    os.chmod(self.config, 0o600)

  def query(self, args, **kwargs):
    self.calls.append(args)
    if args[:2] == ['systemctl', 'show']:
      if args[2] != self.unit:
        return b'inactive\n'
      return (str(self.root / self.unit).encode() if 'FragmentPath' in args else self.state) + b'\n'
    self.assertIn(args, [['systemctl', 'daemon-reload'], ['systemctl', 'start', '--no-block', self.unit]])
    return b''

  def test_park_trial_cannot_overwrite_inactive_home_enrollment(self):
    initial = self.config.read_bytes()
    with patch.dict(os.environ, {}, clear=True), patch.object(remote, 'kernel_resource_support') as check:
      with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'other_mode_enrolled'):
        remote.install(parked=True)
    check.assert_not_called()
    self.assertEqual(self.config.read_bytes(), initial)
    self.assertFalse(self.units.exists())
    self.assertFalse(any('start' in args for args in self.calls))

  def test_runtime_registration_and_simulated_boot_link_loss_restore(self):
    self.assertTrue(remote.restore_runtime()['boot_restore_requested'])
    link = self.units / self.unit
    self.assertTrue(link.is_symlink())
    self.assertEqual(link.resolve(), self.root / self.unit)
    link.unlink()  # Only this fixture's volatile link disappears at a simulated reboot.
    self.assertTrue(remote.restore_runtime()['boot_restore_requested'])
    self.assertTrue(link.is_symlink())
    self.assertFalse(any('enable' in args for args in self.calls))

  def test_active_owned_unit_is_not_restarted_and_foreign_file_is_not_replaced(self):
    remote.runtime_unit()
    self.state = b'active'
    self.assertTrue(remote.restore_runtime()['already_running'])
    self.assertFalse(any('start' in args for args in self.calls))
    link = self.units / self.unit
    link.unlink()
    link.write_text('foreign')
    with self.assertRaisesRegex(ValueError, 'foreign_unit_not_replaced'):
      remote.runtime_unit()
    self.assertEqual(link.read_text(), 'foreign')

  def test_trial_not_enabled_and_bad_qualification_cannot_start(self):
    for changes in ({'trial': True}, {'boot_restore': False}):
      self.write_config(**changes)
      self.assertFalse(remote.restore_runtime()['boot_restore_requested'])
      self.assertFalse(self.units.exists())
    for changes in ({'qualified_modes': ['parked_dashcam']}, {'resource_policy': None}, {'guard_sha256': '0' * 64}, {'binaries': {}}):
      self.write_config(**changes)
      with self.assertRaises(ValueError):
        remote.restore_runtime()
      self.assertFalse(self.units.exists())
    self.assertFalse(any('start' in args for args in self.calls))

  def test_stale_trial_and_changed_interpreter_refuse_boot_restore(self):
    dropin = self.units / (self.unit + '.d/trial.conf')
    dropin.parent.mkdir(parents=True)
    dropin.write_text('[Service]\nRuntimeMaxSec=900s\n')
    with self.assertRaisesRegex(ValueError, 'unexpected_trial_dropin'):
      remote.restore_runtime()
    dropin.unlink()
    (self.root / self.unit).write_text('[Service]\nExecStart=/foreign\n')
    with self.assertRaisesRegex(ValueError, 'qualified_runtime_interpreter_changed'):
      remote.restore_runtime()
    self.assertFalse(any('start' in args for args in self.calls))

  def test_vehicle_runtime_requires_matching_capped_unit_and_separate_mode(self):
    self.write_config(qualified_modes=remote.PARK_MODES, resource_policy=remote.PARK_POLICY)
    with self.assertRaisesRegex(ValueError, 'qualified_runtime_interpreter_changed'):
      remote.restore_runtime()
    (self.root / self.unit).write_bytes(remote.unit_text(sys.executable, remote.PARK_POLICY).encode())
    self.assertTrue(remote.restore_runtime()['boot_restore_requested'])
    self.assertNotIn('RuntimeMaxSec=', (self.root / self.unit).read_text())
    self.write_config(qualified_modes=['detached_home', 'parked_vehicle'], resource_policy=remote.PARK_POLICY)
    with self.assertRaises(ValueError):
      remote.restore_runtime()

  def test_launcher_contains_exact_optional_nonblocking_hook(self):
    launcher = Path(__file__).parents[2] / 'launch_chffrplus.sh'
    source = launcher.read_text(encoding='utf-8')
    self.assertEqual(source.count(remote.BOOT_HOOK), 1)
    self.assertLess(source.index(remote.BOOT_HOOK), source.index('  FORCE_REBUILD=0'))
    self.assertIn('exec 9>&-', remote.BOOT_HOOK)
    self.assertIn('sudo -n env PYTHONPATH=', remote.BOOT_HOOK)
    self.assertIn('restore_supervisor.py', remote.BOOT_HOOK)
    self.assertNotIn('timeout -k 1s 3s', remote.BOOT_HOOK)
    self.assertLess(source.index(remote.BOOT_HOOK), source.index('  # hardware specific init'))
    self.assertIn('/TICI', remote.BOOT_HOOK)

  def test_error_reports_only_stage_errno_and_class(self):
    remote.mark('runtime_unit_link')
    output = io.StringIO()
    with (
      patch.object(sys, 'argv', ['fixture', '--restore-runtime']),
      patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None)}),
      patch.object(remote, 'restore_runtime', side_effect=OSError(errno.EROFS, 'NEVER_PRINT_SECRET_PATH')),
      contextlib.redirect_stdout(output),
    ):
      self.assertEqual(remote.main(), 1)
    value = json.loads(output.getvalue())
    self.assertEqual(value, {'success': False, 'reason': 'OSError', 'stage': 'runtime_unit_link', 'errno': errno.EROFS})
    self.assertNotIn('NEVER_PRINT', output.getvalue())

  def test_authorized_key_uses_checked_runtime_copy_and_rejects_changed_options(self):
    runtime = self.base / 'runtime'
    runtime.mkdir()
    source = self.root / 'conditional_authorized_keys'
    data = ('no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ' + key(1) + '\n').encode()
    source.write_bytes(data)

    def checked(path, **kwargs):
      if path != Path('/'):
        self.owned_path(path, **kwargs)

    with patch.object(remote, 'RUNTIME', runtime), patch.object(remote, 'protected', side_effect=checked):
      remote.runtime_key()
      self.assertEqual((runtime / 'authorized_keys').read_bytes(), data)
      self.assertIn('AuthorizedKeysFile ' + str(runtime / 'authorized_keys'), remote.ssh_config())
      self.assertIn('StrictModes yes', remote.ssh_config())
      source.write_bytes(('command=bad ' + key(1) + '\n').encode())
      with self.assertRaisesRegex(ValueError, 'authorized_key_format'):
        remote.runtime_key()
      source.write_bytes(b'x' * 513)
      with self.assertRaisesRegex(ValueError, 'authorized_key_format'):
        remote.runtime_key()
      self.assertEqual((runtime / 'authorized_keys').read_bytes(), data)


if __name__ == '__main__':
  unittest.main()
