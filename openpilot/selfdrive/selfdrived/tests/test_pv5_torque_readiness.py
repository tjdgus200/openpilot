"""Execute production readiness wiring/checks with socket-free PC fixtures.

No device IPC or full process scheduling is simulated. CarParams uses the actual
schema; SubMaster health methods and event/state predicates are production code.
"""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Optional

import pytest

from openpilot.cereal import car, log
from openpilot.cereal.services import SERVICE_LIST
from openpilot.selfdrive.selfdrived.tests.test_model_startup_events import Events, state_machine

ROOT = Path(__file__).resolve().parents[1]
EventName = log.OnroadEvent.EventName
State = log.SelfdriveState.OpenpilotState


def cp(**changes):
  p = car.CarParams.new_message(brand='hyundai', carFingerprint='KIA_PV5', steerControlType='angle', notCar=False)
  p.lateralTuning.init('pid')
  for key, value in changes.items():
    if key == 'tuning':
      p.lateralTuning.init(value)
    else:
      setattr(p, key, value)
  return p


def make_sm(params=None, simulation=False, replay=False):
  tree = ast.parse((ROOT.parents[1] / 'cereal/messaging/__init__.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SubMaster')
  methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in
             ('all_alive', 'all_freq_ok', 'all_valid', 'all_checks')]
  ns = {'Optional': Optional, 'List': list, 'SERVICE_LIST': SERVICE_LIST}
  exec(compile(ast.Module(body=methods, type_ignores=[]), '<production SubMaster checks>', 'exec'), ns)

  class Messages(dict):
    def __init__(self, services, *, ignore_alive, ignore_avg_freq, ignore_valid, frequency):
      super().__init__(livePose=NS(inputsOK=True, sensorsOK=True, posenetOK=True), liveParameters=NS(valid=True))
      self.services = services
      self.ignore_alive, self.ignore_average_freq, self.ignore_valid = ignore_alive, ignore_avg_freq, ignore_valid
      self.alive = dict.fromkeys(services, True)
      self.freq_ok = dict.fromkeys(services, True)
      self.valid = dict.fromkeys(services, True)
      self.seen = dict.fromkeys(services, True)
  for method in methods:
    setattr(Messages, method.name, ns[method.name])

  tree = ast.parse((ROOT / 'selfdrived.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SelfdriveD')
  init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
  start = next(i for i, n in enumerate(init.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'ignore')
  end = next(i for i, n in enumerate(init.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'self.sm')
  obj = NS(CP=params if params is not None else cp(), sensor_packets=['accelerometer', 'gyroscope'],
           gps_packets=['gpsLocation'], camera_packets=['roadCameraState', 'wideRoadCameraState', 'driverCameraState'],
           use_wide_camera=True)
  ns = dict(self=obj, car=car, SIMULATION=simulation, REPLAY=replay, DT_CTRL=.01, messaging=NS(SubMaster=Messages))
  exec(compile(ast.Module(body=init.body[start:end + 1], type_ignores=[]), '<production readiness wiring>', 'exec'), ns)
  return obj.sm


def health_events(sm, params=None):
  """Actual catch-all and localizer predicates, outside startup settling."""
  tree = ast.parse((ROOT / 'selfdrived.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SelfdriveD')
  method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'update_events')
  start = next(i for i, n in enumerate(method.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'has_disable_events')
  end = next(i for i in range(start, len(method.body)) if isinstance(method.body[i], ast.If) and
             ast.unparse(method.body[i].test) == 'not self.CP.notCar')
  obj = NS(sm=sm, CP=params if params is not None else cp(), events=Events(), logged_comm_issue=None)
  ns = dict(self=obj, EventName=EventName, log=log, num_events=0, model_starting=False, TESTING_CLOSET=False,
            SIMULATION=False, REPLAY=False, cal_status=log.LiveCalibrationData.Status.calibrated,
            ET=NS(NO_ENTRY='NO_ENTRY', SOFT_DISABLE='SOFT_DISABLE', IMMEDIATE_DISABLE='IMMEDIATE_DISABLE'),
            cloudlog=NS(event=lambda *a, **k: None), communication_snapshot=lambda *a: {})
  exec(compile(ast.Module(body=method.body[start:end + 1], type_ignores=[]), '<production health events>', 'exec'), ns)
  return obj.events


@pytest.mark.parametrize('field', ['valid', 'alive', 'freq_ok'])
def test_unused_estimator_does_not_block_and_remains_subscribed(field):
  sm = make_sm()
  getattr(sm, field)['liveTorqueParameters'] = False
  assert 'liveTorqueParameters' in sm.services
  assert sm.all_checks()
  assert not health_events(sm)
  machine = state_machine()
  machine.state = State.enabled
  assert machine.update(health_events(sm)) == (True, True)
  assert machine.state == State.enabled


@pytest.mark.parametrize('changes', [
  {'steerControlType': 'torque', 'tuning': 'torque'}, {'tuning': 'torque'}, {'steerControlType': 'torque'},
  {'carFingerprint': 'KIA_EV6'}, {'carFingerprint': ''}, {'brand': 'mock'}, {'notCar': True},
  {'tuning': 'indiDEPRECATED'}, {'tuning': 'lqrDEPRECATED'},
])
@pytest.mark.parametrize('field', ['valid', 'alive', 'freq_ok'])
def test_other_or_inconsistent_configs_keep_estimator_required(changes, field):
  params = cp(**changes)
  sm = make_sm(params)
  getattr(sm, field)['liveTorqueParameters'] = False
  # No runtime useParams value (including false/default/missing) participates.
  sm['liveTorqueParameters'] = NS(useParams=False)
  assert not sm.all_checks()
  events = health_events(sm, params)
  assert EventName.commIssue in events or EventName.commIssueAvgFreq in events
  machine = state_machine()
  machine.state = State.enabled
  machine.update(events)
  assert machine.state == State.softDisabling


@pytest.mark.parametrize('service', ['modelV2', 'livePose', 'liveCalibration', 'liveParameters', 'driverAssistance',
                                   'pandaStates', 'carOutput', 'controlsState', 'carControl', 'radarState',
                                   'longitudinalPlan', 'managerState', 'deviceState', 'driverMonitoringState',
                                   'roadCameraState', 'wideRoadCameraState', 'driverCameraState'])
@pytest.mark.parametrize('field', ['valid', 'alive', 'freq_ok'])
def test_required_faults_still_block_and_disable(service, field):
  sm = make_sm()
  getattr(sm, field)[service] = False
  assert not sm.all_checks()
  events = health_events(sm)
  machine = state_machine()
  events.add(EventName.buttonEnable)
  assert machine.update(events) == (False, False)
  events.remove(EventName.buttonEnable)
  machine.state = State.enabled
  machine.update(events)
  assert machine.state == State.softDisabling
  for _ in range(300):
    machine.update(events)
  assert machine.state == State.disabled


@pytest.mark.parametrize('field,event', [('inputsOK', 'locationdTemporaryError'), ('posenetOK', 'posenetInvalid')])
def test_localizer_flags_remain_effective(field, event):
  sm = make_sm()
  setattr(sm['livePose'], field, False)
  assert getattr(EventName, event) in health_events(sm)


def test_immediate_disable_overrides_recovery():
  sm = make_sm()
  sm.valid['liveTorqueParameters'] = False
  events = health_events(sm)
  events.add(EventName.controlsMismatch)
  machine = state_machine()
  machine.state = State.softDisabling
  assert machine.update(events) == (False, False)
  assert machine.state == State.disabled


@pytest.mark.parametrize('simulation,replay', [(False, False), (True, False), (False, True)])
def test_other_car_ignore_policy_unchanged(simulation, replay):
  sm = make_sm(cp(carFingerprint='KIA_EV6'), simulation, replay)
  expected = {'accelerometer', 'gyroscope', 'gpsLocation', 'alertDebug', 'liveTracks'}
  if simulation:
    expected |= {'driverCameraState', 'managerState', 'driverMonitoringState'}
  if replay:
    expected |= {'roadCameraState', 'wideRoadCameraState'}
  assert set(sm.ignore_valid) == set(sm.ignore_alive) == set(sm.ignore_average_freq) == expected
@pytest.mark.parametrize('angle', [False, True])
def test_actual_pv5_interface_selects_dependency_from_detected_steering(angle, monkeypatch):
  import sys
  from types import ModuleType

  class MemoryParams:
    def get_bool(self, key):
      return False

    def get_int(self, key):
      return 0

    def get_float(self, key):
      return 0.0

  params_module = ModuleType('openpilot.common.params')
  params_module.Params = MemoryParams
  monkeypatch.setitem(sys.modules, params_module.__name__, params_module)
  from opendbc.car.hyundai.interface import CarInterface
  from opendbc.car.hyundai.values import CAR
  fingerprint = {i: {} for i in range(8)}
  if angle:
    fingerprint[2][0xcb] = 24
  params = CarInterface.get_params(CAR.KIA_PV5, fingerprint, [], True, False, False)
  sm = make_sm(params)
  assert str(params.steerControlType) == ('angle' if angle else 'torque')
  assert params.lateralTuning.which() == ('pid' if angle else 'torque')
  assert ('liveTorqueParameters' in sm.ignore_valid) == angle


def update_event_nodes():
  tree = ast.parse((ROOT / 'selfdrived.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SelfdriveD')
  return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'update_events').body


@pytest.mark.parametrize('counter,rx_bad,expected', [(199, False, False), (200, False, True), (0, True, True)])
def test_panda_mismatch_threshold_and_rx_checks_preserved(counter, rx_bad, expected):
  loop = next(n for n in update_event_nodes() if isinstance(n, ast.For) and
              ast.unparse(n.iter) == "enumerate(self.sm['pandaStates'])")
  params = cp()
  params.init('safetyConfigs', 1)
  params.safetyConfigs[0].safetyModel = 'hyundaiCanfd'
  sm = make_sm(params)
  sm.frame = 2000
  sm['pandaStates'] = [NS(safetyModel=params.safetyConfigs[0].safetyModel, safetyParam=0,
                          alternativeExperience=0, safetyRxChecksInvalid=rx_bad, faults=[])]
  obj = NS(CP=params, sm=sm, mismatch_counter=counter, events=Events())
  exec(compile(ast.Module(body=[loop], type_ignores=[]), '<production Panda checks>', 'exec'),
       dict(self=obj, EventName=EventName, log=log, DT_CTRL=.01, IGNORED_SAFETY_MODES=()))
  assert (EventName.controlsMismatch in obj.events) == expected


@pytest.mark.parametrize('age,expected', [(10.0, False), (10.01, True)])
def test_existing_sensor_timeout_not_ignored(age, expected):
  node = next(n for n in update_event_nodes() if isinstance(n, ast.If) and
              any(isinstance(v, ast.Attribute) and v.attr == 'sensorDataInvalid' for v in ast.walk(n)))
  sm = make_sm()
  sm.frame = round(age * 100)
  sm.recv_frame = {'accelerometer': 0, 'gyroscope': 0}
  obj = NS(sm=sm, sensor_packets=list(sm.recv_frame), events=Events())
  exec(compile(ast.Module(body=[node], type_ignores=[]), '<production sensor timeout>', 'exec'),
       dict(self=obj, EventName=EventName, DT_CTRL=.01))
  assert (EventName.sensorDataInvalid in obj.events) == expected