import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from openpilot.system.pv5_incidents.archive_gate import Admission, Decision, SERVICES, evaluate
from openpilot.system.pv5_incidents.archive_runtime import Coordinator


class SM:
  def __init__(self, now=10):
    self.seen = dict.fromkeys(SERVICES, True)
    self.valid = dict.fromkeys(SERVICES, True)
    self.logMonoTime = dict.fromkeys(SERVICES, int(now * 1e9))
    self.data = {
      'deviceState': NS(
        started=False,
        cpuUsagePercent=[10, 12, 3, 8],
        thermalStatus=NS(raw=0),
        memoryUsagePercent=40,
        freeSpacePercent=50,
        networkType='wifi',
        networkMetered=False,
      ),
      'pandaStates': [NS(pandaType='redPanda', harnessStatus='notConnected', ignitionLine=False, ignitionCan=False)],
      'managerState': NS(
        processes=[NS(name=name, running=False, shouldBeRunning=False) for name in ('card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd')]
        + [NS(name=name, running=True, shouldBeRunning=True) for name in ('pandad', 'hardwared')]
      ),
      'carParams': NS(notCar=False, carFingerprint='KIA_PV5', passive=True, dashcamOnly=False),
      'carState': NS(canValid=True, canTimeout=False, standstill=True, vEgoRaw=0.0, vEgo=0.0, cruiseState=NS(enabled=False), stockAeb=False),
      'carControl': NS(enabled=False, latActive=False, longActive=False),
      'selfdriveState': NS(enabled=False, active=False, state='disabled', alertStatus='normal'),
      'onroadEvents': [],
    }

  def __getitem__(self, key):
    return self.data[key]


class GateChecks(unittest.TestCase):
  def test_home_without_fake_can_and_default_mode_qualification(self):
    sm = SM()
    for key in ('carParams', 'carState', 'carControl', 'selfdriveState', 'onroadEvents'):
      sm.seen[key] = False
      sm.valid[key] = False
    self.assertTrue(evaluate(sm, 10, ['detached_offroad'], True).allowed)
    self.assertFalse(evaluate(sm, 10, [], True).allowed)
    self.assertFalse(evaluate(sm, 10, ['detached_offroad'], False).allowed)

  def test_each_home_stale_future_invalid_unknown_source(self):
    for key, ttl in (('deviceState', 1.5), ('managerState', 1.5), ('pandaStates', 0.25)):
      for mode in ('stale', 'future', 'invalid', 'unseen'):
        sm = SM()
        if mode == 'stale':
          sm.logMonoTime[key] = int((10 - ttl) * 1e9)
        if mode == 'future':
          sm.logMonoTime[key] += 1
        if mode == 'invalid':
          sm.valid[key] = False
        if mode == 'unseen':
          sm.seen[key] = False
        self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)

  def test_home_harness_ignition_controller_or_logger_refuses(self):
    for field, value in (('harnessStatus', 'normal'), ('harnessStatus', 'flipped'), ('ignitionLine', True), ('ignitionCan', True), ('pandaType', 'unknown')):
      sm = SM()
      setattr(sm['pandaStates'][0], field, value)
      self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)
    for name in ('card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd'):
      for field in ('running', 'shouldBeRunning'):
        sm = SM()
        setattr(next(value for value in sm['managerState'].processes if value.name == name), field, True)
        self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)

  def test_non_wifi_metered_resource_or_other_car_refuses(self):
    for field, value in (
      ('networkType', 'cell4G'),
      ('networkType', 'ethernet'),
      ('networkMetered', True),
      ('memoryUsagePercent', 81),
      ('freeSpacePercent', 19),
      ('cpuUsagePercent', []),
      ('cpuUsagePercent', [71]),
      ('cpuUsagePercent', [float('nan')]),
    ):
      sm = SM()
      if field not in ('networkType', 'networkMetered'):
        sm['deviceState'].started = True
      setattr(sm['deviceState'], field, value)
      self.assertFalse(evaluate(sm, 10, ['detached_offroad', 'dashcam_stationary'], True).allowed)
    sm = SM()
    sm['deviceState'].thermalStatus.raw = 1
    self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)
    sm = SM()
    sm['carParams'].carFingerprint = 'OTHER_CAR'
    self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)

  def test_home_load_exception_never_allows_vehicle_or_thermal_conditions(self):
    sm = SM()
    sm['deviceState'].cpuUsagePercent = [100] * 8
    sm['deviceState'].memoryUsagePercent = 99
    sm['deviceState'].freeSpacePercent = 1
    self.assertTrue(evaluate(sm, 10, ['detached_offroad'], True).allowed)
    sm['deviceState'].started = True
    self.assertFalse(evaluate(sm, 10, ['dashcam_stationary'], True).allowed)
    sm['deviceState'].started = False
    sm['deviceState'].thermalStatus.raw = 1
    self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)

  def test_stationary_dashcam_separate_qualification_and_control_gates(self):
    sm = SM()
    sm['deviceState'].started = True
    self.assertTrue(evaluate(sm, 10, ['dashcam_stationary'], True).allowed)
    self.assertFalse(evaluate(sm, 10, ['detached_offroad'], True).allowed)
    for service, field, value in (
      ('carState', 'vEgoRaw', 0.001),
      ('carState', 'vEgo', 0.011),
      ('carState', 'canTimeout', True),
      ('carState', 'canValid', False),
      ('carState', 'stockAeb', True),
      ('carControl', 'enabled', True),
      ('carControl', 'latActive', True),
      ('carControl', 'longActive', True),
      ('selfdriveState', 'active', True),
      ('selfdriveState', 'alertStatus', 'critical'),
    ):
      altered = copy.deepcopy(sm)
      setattr(altered[service], field, value)
      self.assertFalse(evaluate(altered, 10, ['dashcam_stationary'], True).allowed)
    sm['carState'].cruiseState.enabled = True
    self.assertFalse(evaluate(sm, 10, ['dashcam_stationary'], True).allowed)

  def test_two_second_continuity_and_reentry(self):
    gate = Admission()
    for now in (10, 10.5, 11, 11.5):
      self.assertFalse(gate.tick(Decision(True, 'detached_offroad'), now))
    self.assertTrue(gate.tick(Decision(True, 'detached_offroad'), 12))
    self.assertFalse(gate.tick(Decision(False), 12.1))
    self.assertFalse(gate.tick(Decision(True, 'detached_offroad'), 12.2))
    self.assertFalse(gate.tick(Decision(True, 'detached_offroad'), 13.2))
    self.assertFalse(gate.tick(Decision(True, 'detached_offroad'), 13.1))


class LifecycleChecks(unittest.TestCase):
  def test_loss_kills_only_owned_worker_and_descendant(self):
    with tempfile.TemporaryDirectory(prefix='pv5-archive-cancel-test-') as directory:
      root = Path(directory)
      pidfile, script = root / 'pids.json', root / 'worker.py'
      script.write_text(
        '\n'.join(
          [
            'import json,os,subprocess,sys,time',
            "c=subprocess.Popen(['/bin/sleep','60'])",
            "open(sys.argv[1],'w').write(json.dumps([os.getpid(),c.pid]))",
            'while True: time.sleep(.1)',
            '',
          ]
        ),
        encoding='utf-8',
      )
      original = subprocess.Popen

      def spawn(*_args, **kwargs):
        return original([sys.executable, str(script), str(pidfile)], **kwargs)

      coordinator = Coordinator({'qualified_modes': ['detached_offroad'], 'vehicle': 'KIA_PV5'}, '/private/config', '/logs', 'a' * 40)
      with patch('openpilot.system.pv5_incidents.archive_runtime.subprocess.Popen', side_effect=spawn):
        for now in (10, 10.5, 11, 11.5, 12):
          coordinator.tick(SM(now), now)
        for _ in range(40):
          if pidfile.exists():
            break
          time.sleep(0.025)
        pids = json.loads(pidfile.read_text())
        sm = SM(12.1)
        sm['pandaStates'][0].ignitionLine = True
        coordinator.tick(sm, 12.1)
        self.assertIsNone(coordinator.child)
        for _ in range(80):
          # A zombie descendant has stopped consuming CPU/I/O and awaits PID1.
          alive = [
            pid for pid in pids if Path('/proc', str(pid), 'stat').exists() and Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
          ]
          if not alive:
            break
          time.sleep(0.025)
        self.assertEqual(alive, [])


if __name__ == '__main__':
  unittest.main()
