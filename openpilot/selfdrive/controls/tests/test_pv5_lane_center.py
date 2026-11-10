from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.selfdrive.controls.lib.pv5_lane_center import PARAM, Pv5LaneCenter
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
  xs = [0., 20., 60.]
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


def drive(control, cruise_active=True):
  control.sm['carState'].gearShifter = 'drive'
  control.sm['carState'].standstill = False
  control.sm['carState'].vEgo = 20.
  control.sm['selfdriveState'].enabled = control.sm['selfdriveState'].active = cruise_active


@pytest.mark.parametrize('cruise', [False, True])
def test_default_or_legacy_model_message_matches_unchanged_controls(cruise):
  control, _, tick = setup_control(.27, 0)
  other, _, other_tick = setup_control(.27, 0)
  other.pv5_lane_center = None
  for item in (control, other):
    drive(item, cruise)
    item.sm['modelV2'].action.desiredCurvature = .002
  for _ in range(140):
    assert tick().to_dict() == other_tick().to_dict()
  assert control.pv5_lane_center.status == 'off'


@pytest.mark.parametrize('correction', [-.001, .001])
@pytest.mark.parametrize('context', ['ordinary', 'bsd', 'lead', 'dm', 'driver'])
def test_controls_uses_model_action_once_and_reader_does_not_rewrite_it(correction, context):
  control, _, tick = setup_control(.27)
  other, _, other_tick = setup_control(.27)
  other.pv5_lane_center = None
  for item in (control, other):
    drive(item)
    md = item.sm['modelV2']
    md.action.desiredCurvature = .002+correction
    md.pv5LaneCenter.valid = md.pv5LaneCenter.enabled = md.pv5LaneCenter.active = True
    md.pv5LaneCenter.curvature = correction
    md.pv5LaneCenter.trust = .9
    if context == 'bsd':
      item.sm['carState'].rightBlindspot = True
    elif context == 'lead':
      item.sm['radarState'].leadOne.status = True
      item.sm['radarState'].leadOne.dRel = 10.
    elif context == 'dm':
      item.sm['driverMonitoringState'].activePolicy = 'vision'
      item.sm['driverMonitoringState'].visionPolicyState.isDistracted = True
    elif context == 'driver':
      item.sm['carState'].steeringPressed = True
  for _ in range(140):
    assert tick().to_dict() == other_tick().to_dict()
  assert control.pv5_lane_center.status == 'correcting'
  assert control.pv5_lane_center.accel == pytest.approx(correction*400)


@pytest.mark.parametrize('fault', ['can', 'temporary', 'permanent', 'inactive'])
def test_existing_vehicle_lateral_admission_still_disables_actuation(fault):
  control, _, tick = setup_control(.1)
  drive(control)
  control.sm['modelV2'].action.desiredCurvature = .003
  cs = control.sm['carState']
  if fault == 'can':
    cs.canValid = False
  elif fault == 'temporary':
    cs.steerFaultTemporary = True
  elif fault == 'permanent':
    cs.steerFaultPermanent = True
  else:
    cs.latEnabled = False
  assert not tick().latActive
