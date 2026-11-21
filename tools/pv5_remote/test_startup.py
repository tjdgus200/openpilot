"""Startup work must not trip or relax the active source/permission deadlines."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.pv5_remote import conditional_remote as remote
from tools.pv5_remote.test_remote import home


class StartupChecks(unittest.TestCase):
  def test_backend_initializing_then_running_and_invalid_permission(self):
    clock = [0.0]
    checks = []
    permission = SimpleNamespace(check=lambda: checks.append(clock[0]))

    def sleep(seconds):
      clock[0] += seconds

    with (
      patch.object(remote.time, 'monotonic', side_effect=lambda: clock[0]),
      patch.object(remote.time, 'sleep', side_effect=sleep),
      patch.object(remote, 'run', side_effect=[b'{"BackendState":"Starting"}', b'{"BackendState":"Running"}']),
    ):
      remote.wait_backend(['fixture'], permission, 15)
    self.assertEqual(clock[0], 1)
    self.assertEqual(len(checks), 4)
    with patch.object(remote, 'run') as query:
      with self.assertRaisesRegex(ValueError, 'permission_expired'):
        remote.wait_backend(['fixture'], SimpleNamespace(check=lambda: (_ for _ in ()).throw(ValueError('permission_expired'))), remote.time.monotonic() + 1)
      query.assert_not_called()

  def test_non_running_node_is_bounded_without_requesting_authentication(self):
    clock = [0.0]
    with (
      patch.object(remote.time, 'monotonic', side_effect=lambda: clock[0]),
      patch.object(remote.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
      patch.object(remote, 'run', return_value=b'{"BackendState":"Stopped"}') as query,
    ):
      with self.assertRaisesRegex(ValueError, '^node_not_running$'):
        remote.wait_backend(['fixture'], SimpleNamespace(check=lambda: None), 3)
    self.assertEqual(query.call_count, 3)
    self.assertFalse(any('login' in str(call) or 'up' in str(call) for call in query.call_args_list))

  def test_slow_foreign_check_precedes_admission_and_trial_exit_is_bounded(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      (root / 'conditional_modes.json').write_text(json.dumps({
        'trial': True, 'qualified_modes': ['detached_home'], 'resource_policy': remote.HOME_POLICY, 'binaries': {},
      }))
      proc = root / 'cgroup'
      proc.write_text('0::/system.slice/' + remote.UNIT + '\n')
      cg = root / 'sys/system.slice' / remote.UNIT
      cg.mkdir(parents=True)
      (cg / 'memory.max').write_text('max')
      (cg / 'cpu.max').write_text('max 100000')
      limits = remote.resource_limits
      clock = [0.0]
      opened = []
      scan = []

      class FakeEntry:
        def __truediv__(self, name):
          self_test.assertEqual(name, 'comm')
          return self

        def read_text(self):
          scan.append(clock[0])
          clock[0] += 0.22
          return 'fixture'

      self_test = self

      def glob(path, pattern):
        self.assertEqual(path, Path('/proc'))
        self.assertEqual(pattern, '[0-9]*')
        return iter([FakeEntry()])

      class Source:
        def read(self):
          return home(clock[0])

      class Input:
        def fileno(self):
          return 123

        def close(self):
          pass

      child = SimpleNamespace(stdin=Input(), poll=lambda: None)

      def spawn(*args, **kwargs):
        opened.append(clock[0])
        return child

      def sleep(seconds):
        clock[0] += seconds
        if clock[0] > 3:
          (root / 'stop-trial').touch()

      output = io.StringIO()
      with (
        patch.object(remote, 'ROOT', root),
        patch.object(remote, 'RUNTIME', root),
        patch.object(remote, 'prerequisites', return_value={}),
        patch.object(remote, 'protected'),
        patch.object(remote, 'resource_limits', side_effect=lambda cfg: limits(cfg, proc, root / 'sys')),
        patch.object(remote, 'Source', Source),
        patch.object(remote, 'notify'),
        patch.object(remote.time, 'monotonic', side_effect=lambda: clock[0]),
        patch.object(remote.time, 'sleep', side_effect=sleep),
        patch.object(remote.os, 'PRIO_PROCESS', 0, create=True),
        patch.object(remote.os, 'setpriority', create=True),
        patch.object(remote.os, 'sched_getaffinity', return_value={0}, create=True),
        patch.object(remote.os, 'sched_setaffinity', create=True),
        patch.object(remote.os, 'set_blocking', create=True),
        patch.object(remote.os, 'write'),
        patch.object(remote.subprocess, 'Popen', side_effect=spawn),
        patch.object(Path, 'glob', glob),
        contextlib.redirect_stdout(output),
      ):
        remote.guard()
      self.assertEqual(len(scan), 1)
      self.assertEqual(len(opened), 1)
      self.assertGreaterEqual(opened[0] - scan[0], 2)
      self.assertEqual(json.loads(output.getvalue())['trial_exit'], 'trial_requested_stop')
      self.assertEqual(len(output.getvalue().splitlines()), 1)

  def test_qualified_park_toggle_blocks_admission_then_closes_only_its_child(self):
    import hashlib

    from openpilot.common import pv5_park_ssh
    from tools.pv5_remote.test_remote import parked

    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      script = root / 'conditional_remote.py'
      script.write_bytes(b'fixture')
      (root / 'conditional_modes.json').write_text(json.dumps({
        'trial': False, 'boot_restore': True, 'qualified_modes': remote.PARK_MODES,
        'resource_policy': remote.PARK_POLICY, 'binaries': {},
        'guard_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
      }))
      clock = [0.]
      opened = []
      closed = []
      writes = []

      def read():
        self.assertTrue((root / pv5_park_ssh.MARKER_NAME).is_file())
        sample = parked()
        for row in sample.values():
          row['stamp'] = clock[0]
        return sample

      def spawn(*args, **kwargs):
        opened.append(clock[0])
        stdin = SimpleNamespace(fileno=lambda: 123, close=lambda: closed.append(clock[0]))
        return SimpleNamespace(stdin=stdin, poll=lambda: None)

      def sleep(seconds):
        clock[0] += seconds
        self.assertLess(clock[0], 12, 'guard did not exit after OFF')

      with (
        patch.object(remote, 'ROOT', root),
        patch.object(remote, 'RUNTIME', root),
        patch.object(remote, 'SCRIPT', script),
        patch.object(remote, 'prerequisites', return_value={}),
        patch.object(remote, 'protected'),
        patch.object(remote, 'resource_limits') as limits,
        patch.object(remote, 'Source', lambda: SimpleNamespace(read=read)),
        patch.object(remote, 'notify'),
        patch.object(remote.time, 'monotonic', side_effect=lambda: clock[0]),
        patch.object(remote.time, 'sleep', side_effect=sleep),
        patch.object(remote.os, 'PRIO_PROCESS', 0, create=True),
        patch.object(remote.os, 'setpriority', create=True),
        patch.object(remote.os, 'set_blocking', create=True),
        patch.object(remote.os, 'write', side_effect=lambda *args: writes.append(clock[0])),
        patch.object(remote.subprocess, 'Popen', side_effect=spawn),
        patch.object(Path, 'glob', return_value=iter(())),
        patch.object(pv5_park_ssh, 'requested', side_effect=lambda: 1 <= clock[0] < 2 or 4 <= clock[0] < 7),
      ):
        remote.guard()
      limits.assert_called_once()
      self.assertEqual(len(opened), 1)
      self.assertGreaterEqual(opened[0], 6)
      self.assertLess(writes[-1], 7)
      self.assertEqual(len(closed), 1)
      self.assertLessEqual(closed[0], 7.11)
      self.assertFalse((root / pv5_park_ssh.MARKER_NAME).exists())


if __name__ == '__main__':
  unittest.main()
