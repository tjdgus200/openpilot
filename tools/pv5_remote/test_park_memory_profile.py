"""Offline park-registration and effective-resource checks. No network or device."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.pv5_remote import conditional_remote as remote
from tools.pv5_remote.test_remote import parked


class ParkRegistrationTests(unittest.TestCase):
  def test_guard_uses_high_memory_exception_only_for_one_time_sidecar_trial(self):
    sample = parked(10)
    sample['deviceState']['memory'] = 95
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      script = root / 'guard.py'
      script.write_bytes(b'fixture')
      for sidecar, trial in ((True, True), (True, False), (False, True), (False, False)):
        cfg = {'qualified_modes': remote.PARK_MODES, 'resource_policy': remote.PARK_COMPAT_POLICY if sidecar else remote.PARK_POLICY,
               'binaries': {}, 'trial': trial, 'boot_restore': not trial, 'guard_sha256': hashlib.sha256(script.read_bytes()).hexdigest()}
        if sidecar:
          cfg['park_sidecar'] = True
        (root / 'conditional_modes.json').write_text(json.dumps(cfg))
        with (
          self.subTest(sidecar=sidecar, trial=trial),
          patch.object(remote, 'ROOT', root), patch.object(remote, 'RUNTIME', root), patch.object(remote, 'SCRIPT', script),
          patch.object(remote, 'PARK_SIDECAR', sidecar), patch.object(remote, 'prerequisites', return_value={}),
          patch.object(remote, 'protected'), patch.object(remote, 'resource_limits'), patch.object(remote, 'notify'),
          patch.object(remote.os, 'PRIO_PROCESS', 0, create=True), patch.object(remote.os, 'setpriority', create=True),
          patch.object(remote, 'Source', return_value=SimpleNamespace(read=lambda: sample)),
          patch.object(remote.time, 'monotonic', return_value=10),
          patch.object(remote, 'eligible', side_effect=StopIteration) as called,
          self.assertRaises(StopIteration),
        ):
          remote.guard()
        args, kwargs = called.call_args
        self.assertEqual(bool(remote.eligible(*args, **kwargs)[0]), sidecar and trial)

  def test_registration_relaxes_only_valid_numeric_memory_and_only_in_park(self):
    for memory in (80, 81, 95, 100, 101, -1, float('nan'), None):
      sample = parked()
      sample['deviceState']['memory'] = memory
      with self.subTest(memory=memory):
        self.assertEqual(bool(remote.eligible(sample, 10, remote.PARK_MODES, registration=True)[0]), memory in (80, 81, 95, 100))
        self.assertEqual(bool(remote.eligible(sample, 10, remote.PARK_MODES)[0]), memory == 80)
        self.assertEqual(bool(remote.eligible(sample, 10, ['parked_dashcam'], registration=True)[0]), memory == 80)

  def test_high_memory_registration_preserves_vehicle_event_and_resource_gates(self):
    for service, field, value in (
      ('carState', 'gear', 'drive'), ('carState', 'raw_speed', 0.001), ('carState', 'speed', -0.001),
      ('carState', 'standstill', False), ('carState', 'can_valid', False), ('carState', 'aeb', True),
      ('carControl', 'lat', True), ('selfdriveState', 'critical', True), ('onroadEvents', 'disable', True),
      ('deviceState', 'cpu', [71]), ('deviceState', 'thermal', 1), ('deviceState', 'free', 19),
      ('pandaStates', 'valid', False), ('managerState', 'valid', False), ('carParams', 'vehicle', 'OTHER'),
      ('carParams', 'stamp', 45), ('carControl', 'stamp', 99.7), ('onroadEvents', 'stamp', 97.5),
    ):
      with self.subTest(service=service, field=field):
        sample = parked(100)
        sample['deviceState']['memory'] = 91
        sample[service][field] = value
        self.assertFalse(remote.eligible(sample, 100, remote.PARK_MODES, registration=True)[0])

  def test_await_requires_two_seconds_continuous_fresh_park_at_high_memory(self):
    clock = [0.]

    def read():
      sample = parked(clock[0])
      sample['deviceState']['memory'] = 85
      sample['carControl']['enabled'] = clock[0] < 1
      return sample

    def advance(dt):
      clock[0] += dt

    self.assertEqual(remote.await_eligible(SimpleNamespace(read=read), remote.PARK_MODES, registration=True,
                                          clock=lambda: clock[0], pause=advance), 'parked_vehicle')
    self.assertGreaterEqual(clock[0], 3)
    self.assertFalse(remote.eligible(read(), clock[0], remote.PARK_MODES)[0])


class EffectiveCompatibilityLimits(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    self.sys = root / 'cgroup'
    self.base = self.sys / 'system.slice' / remote.PARK_UNIT
    self.base.mkdir(parents=True)
    self.controllers = self.sys / 'cgroup.controllers'
    self.controllers.write_text('memory\n')
    self.proc = root / 'self/cgroup'
    self.proc.parent.mkdir()
    self.proc.write_text('0::/system.slice/' + remote.PARK_UNIT)
    (self.proc.parent / 'status').write_text('NoNewPrivs:\t1\n')
    (self.proc.parent / 'oom_score_adj').write_text('500\n')
    (self.base / 'memory.max').write_text('268435456\n')
    self.cfg = {'qualified_modes': remote.PARK_MODES, 'resource_policy': remote.PARK_COMPAT_POLICY}
    self.enter_mock(patch.object(remote, 'PARK_SIDECAR', True))
    self.enter_mock(patch.object(remote, 'UNIT', remote.PARK_UNIT))
    self.nice = self.enter_mock(patch.object(remote.os, 'getpriority', return_value=19, create=True))
    self.enter_mock(patch.object(remote.os, 'PRIO_PROCESS', 0, create=True))
    self.affinity = self.enter_mock(patch.object(remote.os, 'sched_getaffinity', return_value={0, 3}, create=True))
    self.rlimits = {'fd': (256, 256), 'core': (0, 0)}
    fake = SimpleNamespace(getrlimit=self.rlimits.__getitem__, RLIMIT_NOFILE='fd', RLIMIT_CORE='core')
    self.enter_mock(patch.dict(sys.modules, {'resource': fake}))

  def enter_mock(self, patcher):
    self.addCleanup(patcher.stop)
    return patcher.start()

  def read(self):
    return remote.resource_limits(self.cfg, self.proc, self.sys)

  def test_controller_selection_refuses_partial_support_and_keeps_full_profile(self):
    for controllers in ('memory', 'memory cpu pids', 'cpu pids', 'memory pids', 'memory cpu'):
      self.controllers.write_text(controllers)
      if controllers == 'memory':
        self.assertEqual(remote.kernel_resource_support(self.sys), remote.PARK_COMPAT_POLICY)
      elif controllers == 'memory cpu pids':
        self.assertEqual(remote.kernel_resource_support(self.sys), remote.PARK_POLICY)
      else:
        with self.assertRaises(ValueError):
          remote.kernel_resource_support(self.sys)
    self.controllers.write_text('memory pids')
    (self.sys / 'cpu,cpuacct').mkdir()
    (self.sys / 'cpu,cpuacct/cpu.cfs_quota_us').write_text('-1')
    self.assertEqual(remote.kernel_resource_support(self.sys), remote.PARK_POLICY)

  def test_proof_is_measured_and_does_not_claim_unavailable_quotas(self):
    self.assertEqual(self.read(), {'resource_policy': remote.PARK_COMPAT_POLICY, 'memory_limit': 268435456,
                                  'CPU_quota_limit': None, 'CPU_controller': None, 'tasks_limit': None, 'nice': 19,
                                  'cpu_affinity': [0, 3], 'oom_score_adj': 500, 'nofile_limit': 256, 'core_limit': 0,
                                  'no_new_privs': True})
    unit = remote.unit_text('/usr/bin/python3', remote.PARK_COMPAT_POLICY)
    for line in ('MemoryMax=256M', 'CPUAffinity=0 1 2 3', 'OOMScoreAdjust=500', 'Nice=19', 'NoNewPrivileges=yes',
                 'LimitNOFILE=256', 'LimitCORE=0', 'KillMode=control-group'):
      self.assertIn(line, unit.splitlines())
    self.assertNotIn('CPUQuota=', unit)
    self.assertNotIn('TasksMax=', unit)
    self.cfg['qualified_modes'] = ['detached_home']
    with self.assertRaises(ValueError):
      self.read()

  def test_requires_exact_memory_cap_matching_unit_and_unavailable_controllers(self):
    with patch.object(remote, 'PARK_SIDECAR', False):
      with self.assertRaisesRegex(ValueError, 'park_sidecar_required'):
        self.read()
      with self.assertRaisesRegex(ValueError, 'park_sidecar_required'):
        remote.unit_text('/usr/bin/python3', remote.PARK_COMPAT_POLICY)
    for cap in ('max', '0', '134217728', '268435457'):
      (self.base / 'memory.max').write_text(cap)
      with self.assertRaisesRegex(ValueError, 'vehicle_memory_limit_required'):
        self.read()
    (self.base / 'memory.max').write_text('268435456')
    for controller, setting in (('cpu.max', 'max 100000'), ('pids.max', 'max')):
      (self.base / controller).write_text(setting)
      with self.assertRaisesRegex(ValueError, 'vehicle_resource_profile_changed'):
        self.read()
      (self.base / controller).unlink()
    self.proc.write_text('0::/system.slice/pv5-conditional-remote.service')
    with self.assertRaisesRegex(ValueError, 'guard_cgroup_required'):
      self.read()

  def test_fail_closed_on_effective_process_settings(self):
    for field, value in ((self.nice, 18), (self.affinity, {4}), (self.affinity, set())):
      original = field.return_value
      field.return_value = value
      with self.assertRaisesRegex(ValueError, 'park_process_protection_required'):
        self.read()
      field.return_value = original
    for limit, value in (('fd', (256, 1024)), ('core', (0, -1))):
      original = self.rlimits[limit]
      self.rlimits[limit] = value
      with self.assertRaisesRegex(ValueError, 'park_process_protection_required'):
        self.read()
      self.rlimits[limit] = original
    (self.proc.parent / 'oom_score_adj').write_text('0')
    with self.assertRaisesRegex(ValueError, 'park_process_protection_required'):
      self.read()
    (self.proc.parent / 'oom_score_adj').write_text('500')
    (self.proc.parent / 'status').write_text('NoNewPrivs:\t0\n')
    with self.assertRaisesRegex(ValueError, 'park_process_protection_required'):
      self.read()
