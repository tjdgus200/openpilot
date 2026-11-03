import math
from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.selfdrive.controls.lib.pv5_lane_center import MAX_CORRECTION_ACCEL, PARAM, Pv5LaneCenter
from openpilot.selfdrive.controls.tests.test_lateral_startup_control import control_fixture


def setup_control(offset, selected=1):
  clock = SimpleNamespace(now=100.0)
  control, step = control_fixture(monotonic=lambda: clock.now)
  settings = {PARAM: selected}
  get_bool = control.params.get_bool
  control.params.get_bool = lambda key: settings[key] == 1 if key in settings else get_bool(key)
  control.pv5_lane_center = Pv5LaneCenter(control.params)
  sm = control.sm
  cs = sm['carState']
  cs.canValid = True
  cs.gearShifter = 'park'
  cs.standstill = True
  cs.vEgo = 0.
  cs.steeringAngleDeg = 0.
  sm['radarState'] = log.RadarState.new_message()
  sm['driverMonitoringState'] = log.DriverMonitoringState.new_message()
  model = sm['modelV2']
  xs = [0., 20., 45.]
  model.position.x = xs
  model.position.y = [0.] * len(xs)
  model.init('laneLines', 4)
  model.laneLineProbs = [0., .99, .99, 0.]
  model.laneLineStds = [0., .1, .1, 0.]
  for i, y in ((1, offset - 1.7), (2, offset + 1.7)):
    model.laneLines[i].x = xs
    model.laneLines[i].y = [y] * len(xs)
  model.init('leadsV3', 3)
  sm.seen.update(radarState=True, driverMonitoringState=True)
  sm.valid.update(radarState=True, driverMonitoringState=True)
  sm.alive.update(radarState=True, driverMonitoringState=True)
  def tick(*, new_model=True, repeated=False, stale=None):
    clock.now += .01
    sm.frame += 1
    if new_model:
      model.frameId += 1
    if new_model or repeated:
      sm.recv_frame['modelV2'] = sm.frame
    sm.recv_time = dict.fromkeys(sm, clock.now)
    if stale:
      sm.recv_time[stale] -= .3
    return step()[0]

  return control, settings, tick


def drive(control):
  control.sm['carState'].gearShifter = 'drive'
  control.sm['carState'].standstill = False
  control.sm['carState'].vEgo = 20.
  control.sm['selfdriveState'].enabled = control.sm['selfdriveState'].active = True


def test_disabled_matches_existing_control():
  control, _, tick = setup_control(.27, selected=0)
  baseline, _, baseline_tick = setup_control(.27, selected=0)
  baseline.pv5_lane_center = None
  tick()
  baseline_tick()
  for instance in (control, baseline):
    drive(instance)
    instance.sm['modelV2'].action.desiredCurvature = .001
  for _ in range(110):
    assert tick().to_dict() == baseline_tick().to_dict()


@pytest.mark.parametrize('offset', [-.27, .27])
def test_full_control_loop_bounded_correction_and_release(offset):
  control, settings, tick = setup_control(offset)
  tick()
  assert control.pv5_lane_center.enabled
  drive(control)
  for i in range(200):
    cc = tick(new_model=i % 5 == 0)
    assert cc.latActive and math.isfinite(cc.actuators.torque)
    assert abs(cc.actuators.curvature) * 20**2 <= MAX_CORRECTION_ACCEL + 1e-6
    if i < 49:
      assert cc.actuators.curvature == 0
  assert cc.actuators.curvature * 20**2 == pytest.approx(math.copysign(.06, offset), abs=.001)
  settings[PARAM] = 0
  for i in range(26):
    tick(new_model=i % 5 == 0)
  assert not control.pv5_lane_center.enabled
  assert control.pv5_lane_center.target == 0.
  for i in range(90):
    cc = tick(new_model=i % 5 == 0)
  assert not control.pv5_lane_center.enabled
  assert abs(cc.actuators.curvature) * 20**2 < .001
  settings[PARAM] = 1
  for i in range(26):
    tick(new_model=i % 5 == 0)
  assert control.pv5_lane_center.enabled
  for i in range(130):
    cc = tick(new_model=i % 5 == 0)
  assert cc.actuators.curvature * 20**2 == pytest.approx(math.copysign(.06, offset), abs=.001)


@pytest.mark.parametrize('condition', ['blinker', 'blindspot', 'driver', 'radar', 'radar_fault', 'model_lead', 'desire',
                                      'lane_mode', 'stale', 'repeated', 'invalid_line', 'no_cruise'])
def test_rejects_correction_and_resets_dwell(condition):
  control, _, tick = setup_control(.27)
  tick()
  drive(control)
  sm = control.sm
  for i in range(140):
    tick(new_model=i % 5 == 0)
  assert control.pv5_lane_center.target > 0.
  match condition:
    case 'blinker':
      sm['carState'].leftBlinker = True
    case 'blindspot':
      sm['carState'].rightBlindspot = True
    case 'driver':
      sm['carState'].steeringPressed = True
    case 'radar':
      sm['radarState'].leadTwo.status = True
      sm['radarState'].leadTwo.dRel = 40.
    case 'radar_fault':
      sm['radarState'].radarErrors.radarFault = True
    case 'model_lead':
      sm['modelV2'].leadsV3[1].prob = .5
    case 'desire':
      sm['modelV2'].meta.desire = 'turnLeft'
    case 'lane_mode':
      sm['lateralPlan'].useLaneLines = True
    case 'invalid_line':
      sm['modelV2'].laneLines[2].y = [1.97, float('nan'), 1.97]
    case 'no_cruise':
      sm['selfdriveState'].active = False
  tick(new_model=condition != 'repeated', repeated=condition == 'repeated',
       stale='modelV2' if condition == 'stale' else None)
  assert control.pv5_lane_center.target == 0.
  assert control.pv5_lane_center.eligible_since is None
