"""Run the actual PC probe with isolated kernel-file and socket fixtures."""

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.pv5_remote.conditional_remote import HOME_POLICY
from tools.pv5_remote.connect import CHECK


class ProbeChecks(unittest.TestCase):
  def probe(self, cpu=None, memory='max', modes=None, policy=HOME_POLICY):
    with tempfile.TemporaryDirectory() as directory:
      base = Path(directory)
      group = '/system.slice/pv5-conditional-remote.service'
      root = base / 'cgroup' / group.lstrip('/')
      root.mkdir(parents=True)
      (root / 'memory.max').write_text(memory)
      (root / 'memory.current').write_text('42424242')
      if cpu is not None:
        (root / 'cpu.max').write_text(cpu)
      (base / 'group').write_text('0::' + group)
      (base / 'config').write_text(json.dumps({'qualified_modes': ['detached_home'] if modes is None else modes,
                                             'resource_policy': policy, 'trial': True}))
      (base / 'guard').write_text('public guard fixture\n')
      source = CHECK
      for old, new in [('/proc/self/cgroup', base / 'group'), ('/sys/fs/cgroup', base / 'cgroup'),
                       ('/data/pv5-remote-access/conditional_modes.json', base / 'config'),
                       ('/data/pv5-remote-access/conditional_remote.py', base / 'guard')]:
        source = source.replace(old, str(new).replace('\\', '/'))
      output = io.StringIO()
      with (patch.object(os, 'geteuid', return_value=0, create=True),
            patch('os.open', side_effect=OSError(errno.EROFS, 'fixture readonly')),
            patch('socket.AF_UNIX', 1, create=True),
            patch('socket.socket') as socket,
            patch('time.sleep'), contextlib.redirect_stdout(output)):
        socket.return_value.connect.side_effect = OSError(errno.ECONNREFUSED, 'fixture blocked')
        exec(compile(source, '<remote-probe-fixture>', 'exec'), {})
      return json.loads(output.getvalue())

  def test_home_uncapped_works_without_cpu_controller(self):
    result = self.probe()
    self.assertFalse(result['CPU_controller_available'])
    self.assertIsNone(result['CPU_quota_limit'])
    self.assertIsNone(result['memory_limit'])
    self.assertEqual(result['resource_policy'], HOME_POLICY)

  def test_available_cpu_controller_must_have_no_service_quota(self):
    self.assertTrue(self.probe(cpu='max 100000')['CPU_controller_available'])
    with self.assertRaisesRegex(AssertionError, 'unexpected_CPU_cap'):
      self.probe(cpu='10000 100000')

  def test_leftover_memory_cap_is_not_called_uncapped(self):
    with self.assertRaisesRegex(AssertionError, 'unexpected_memory_cap'):
      self.probe(memory='268435456')

  def test_vehicle_mixed_unknown_and_old_policies_refuse_attestation(self):
    for modes, policy in [(['parked_dashcam'], HOME_POLICY), (['detached_home', 'parked_dashcam'], HOME_POLICY),
                          (['detached_home'], None), (['detached_home'], 'old_capped'), ([], HOME_POLICY)]:
      with self.assertRaisesRegex(AssertionError, 'home_resource_policy'):
        self.probe(modes=modes, policy=policy)


if __name__ == '__main__':
  unittest.main()
