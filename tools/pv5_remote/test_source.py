"""Real full-schema transport decoding with in-memory subscribers only."""

import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from tools.pv5_archive.schema import full_schema
from tools.pv5_remote.conditional_remote import SERVICES, Source, eligible


class SourceChecks(unittest.TestCase):
  def test_real_native_bool_messages_home_normal_zero_and_dashcam(self):
    with full_schema() as log:
      events = {}
      for name in SERVICES:
        event = log.Event.new_message(valid=True, logMonoTime=10000000000)
        size = 1 if name == 'pandaStates' else 0
        state = event.init(name, size) if name in ('pandaStates', 'onroadEvents') else event.init(name)
        if name == 'deviceState':
          state.started, state.cpuUsagePercent, state.memoryUsagePercent, state.freeSpacePercent = False, [5, 7], 40, 60.0
          state.thermalStatus = 'green'
        elif name == 'pandaStates':
          state[0].pandaType, state[0].harnessStatus = 'redPanda', 'notConnected'
        elif name == 'managerState':
          names = ('card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd', 'hardwared', 'pandad')
          for row, process in zip(state.init('processes', len(names)), names, strict=True):
            row.name = process
            row.running = row.shouldBeRunning = process in ('hardwared', 'pandad')
        elif name == 'carParams':
          state.carFingerprint, state.notCar, state.dashcamOnly = 'KIA_PV5', False, False
        elif name == 'carState':
          state.canValid, state.canTimeout, state.standstill, state.gearShifter = True, False, True, 'park'
        elif name == 'selfdriveState':
          state.state, state.alertStatus = 'disabled', 'normal'
        events[name] = event

      def packet(value):
        value.clear_write_flag()
        return value.to_bytes()

      pending = {name: [packet(value)] for name, value in events.items()}

      class Reader:
        def __init__(self, name):
          self.name = name

        def receive(self, non_blocking=True):
          return pending[self.name].pop(0) if pending[self.name] else None

      cereal = ModuleType('openpilot.cereal')
      cereal.log, cereal.messaging = log, SimpleNamespace(sub_sock=lambda name, **_: Reader(name))
      with patch.dict(sys.modules, {'openpilot.cereal': cereal}):
        source = Source()
        self.assertEqual(eligible(source.read(), 10, ['detached_home']), ('detached_home', ''))
        events['deviceState'].deviceState.started = True
        pending['deviceState'] = [packet(events['deviceState'])]
        self.assertEqual(eligible(source.read(), 10, ['parked_dashcam']), ('', 'normal_mode_code_reports_only'))
        events['carParams'].carParams.dashcamOnly = True
        pending['carParams'] = [packet(events['carParams'])]
        self.assertEqual(eligible(source.read(), 10, ['parked_dashcam']), ('parked_dashcam', ''))
        events['carControl'].carControl.longActive = True
        pending['carControl'] = [packet(events['carControl'])]
        self.assertFalse(eligible(source.read(), 10, ['parked_dashcam'])[0])


if __name__ == '__main__':
  unittest.main()
