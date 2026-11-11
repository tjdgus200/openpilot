from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.selfdrive.controls.lib.pv5_lane_boundary import PARAM, Pv5LaneBoundary
from openpilot.selfdrive.controls.tests.test_lateral_startup_control import control_fixture


def setup_control(offset, selected=1):
  clock = SimpleNamespace(now=100.0)
  control, step = control_fixture(monotonic=lambda: clock.now)
  settings = {PARAM: selected}
  get_bool = control.params.get_bool
  control.params.get_bool = lambda key: settings[key] == 1 if key in settings else get_bool(key)
  control.pv5_lane_center = Pv5LaneBoundary(control.params)
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
  xs = [0., 5., 10., 15., 20., 40., 60.]
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
    last_model_time = getattr(sm, 'recv_time', {}).get('modelV2', clock.now)
    sm.recv_time = dict.fromkeys(sm, clock.now)
    if not (new_model or repeated):
      sm.recv_time['modelV2'] = last_model_time
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


@pytest.mark.parametrize('offset', [-.65, .65])
@pytest.mark.parametrize('context', ['ordinary', 'bsd', 'lead', 'dm'])
def test_controls_applies_inward_edge_correction_without_rewriting_model(offset, context):
  control, _, tick = setup_control(offset)
  other, _, other_tick = setup_control(offset)
  other.pv5_lane_center = None
  for item in (control, other):
    drive(item)
    md = item.sm['modelV2']
    md.action.desiredCurvature = 0.
    if context == 'bsd':
      item.sm['carState'].rightBlindspot = True
    elif context == 'lead':
      item.sm['radarState'].leadOne.status = True
      item.sm['radarState'].leadOne.dRel = 10.
    elif context == 'dm':
      item.sm['driverMonitoringState'].activePolicy = 'vision'
      item.sm['driverMonitoringState'].visionPolicyState.isDistracted = True
  for _ in range(140):
    cc = tick()
    other_tick()
    assert control.sm['modelV2'].action.desiredCurvature == 0.
    assert offset*cc.actuators.curvature >= 0
  assert control.pv5_lane_center.status == 'correcting'
  assert abs(control.pv5_lane_center.accel) <= .30
  assert offset*(control.desired_curvature-other.desired_curvature) > 0


@pytest.mark.parametrize('offset', [-.4, 0., .4])
def test_inside_lane_uses_exact_baseline_control_output(offset):
  control, _, tick = setup_control(offset)
  other, _, other_tick = setup_control(offset)
  other.pv5_lane_center = None
  drive(control)
  drive(other)
  for _ in range(140):
    assert tick().to_dict() == other_tick().to_dict()
  assert control.pv5_lane_center.status == 'centered'


@pytest.mark.parametrize('context', ['driver', 'leftBlinker', 'rightBlinker'])
def test_active_edge_push_yields_immediately_to_driver_and_signals(context):
  control, _, tick = setup_control(.65)
  drive(control)
  for _ in range(100):
    tick()
  assert control.pv5_lane_center.accel > 0
  setattr(control.sm['carState'], 'steeringPressed' if context == 'driver' else context, True)
  tick()
  assert control.pv5_lane_center.accel == 0


def test_missing_selected_lane_plan_clears_cached_push_and_status():
  control, _, tick = setup_control(.65)
  drive(control)
  for _ in range(100):
    tick()
  assert control.pv5_lane_center.accel > 0
  control.sm['lateralPlan'].useLaneLines = True
  control.sm['lateralPlan'].curvatures = []
  tick()
  assert control.pv5_lane_center.accel == 0
  assert control.pv5_lane_center.status == 'inputs'


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
