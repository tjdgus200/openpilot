import ast
import math
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.selfdrive.controls.lib.pv5_lane_boundary import HALF_BODY, MAX_ACCEL, MARGIN, Pv5LaneBoundary


def scenario(offset=0., speed=20., k=0., enabled=True):
  x = np.linspace(0., 100., 33)
  model = NS(frameId=0, laneLineProbs=[0., .99, .99, 0.], laneLineStds=[0., .1, .1, 0.],
             laneLines=[NS(x=x, y=offset+y+k*x*x/2) for y in (-5.1, -1.7, 1.7, 5.1)],
             meta=NS(desire='none', laneChangeState='off', autoTurnSignal='none'))
  cs = NS(vEgo=speed, canValid=True, canTimeout=False, gearShifter='drive', standstill=False,
          leftBlinker=False, rightBlinker=False, steeringPressed=False)
  sm = NS(seen={'carState': True, 'modelV2': True}, valid={'carState': True, 'modelV2': True},
          alive={'carState': True, 'modelV2': True}, recv_time={})
  settings = [enabled]
  owner = Pv5LaneBoundary(NS(get_bool=lambda _: settings[0]))
  clock = [100.]

  def tick(curvature=k, *, active=True, fresh=True):
    clock[0] += .01
    sm.recv_time['carState'] = clock[0]
    if fresh or 'modelV2' not in sm.recv_time:
      sm.recv_time['modelV2'] = clock[0]
      model.frameId += 1
    return owner.update(sm, cs, model, clock[0], active=active, curvature=curvature)
  return owner, cs, model, sm, settings, tick


@pytest.mark.parametrize('offset', [-.50, -.25, 0., .25, .50])
@pytest.mark.parametrize('speed', [5., 10., 20., 30.])
@pytest.mark.parametrize('k', [-.002, 0., .002])
def test_inside_corridor_is_exactly_zero_even_off_center_and_in_curves(offset, speed, k):
  owner, _, _, _, _, tick = scenario(offset, speed, k)
  for _ in range(60):
    assert tick() == 0.
  assert owner.status == 'centered'


@pytest.mark.parametrize('side', [-1., 1.])
@pytest.mark.parametrize('gap', [-.1, 0., .1, MARGIN-.001])
@pytest.mark.parametrize('speed', [5., 10., 20., 30.])
def test_near_or_touching_edge_only_pushes_inward_and_is_bounded(side, gap, speed):
  owner, _, _, _, _, tick = scenario(side*(1.7-HALF_BODY-gap), speed)
  values = [tick() for _ in range(120)]
  assert side*values[-1] > 0
  assert max(abs(x)*speed**2 for x in values) <= MAX_ACCEL+1e-9
  assert max(np.diff(np.abs(values)))*speed**2/.01 <= .5+1e-8
  assert owner.status == 'correcting'


@pytest.mark.parametrize('side', [-1., 1.])
def test_predicted_drift_qualifies_but_already_inward_path_is_not_opposed(side):
  owner, _, _, _, _, tick = scenario()
  for _ in range(90):
    assert side*tick(-side*.01) >= 0
  assert side*owner.accel > 0
  assert tick(0.) == 0.
  assert owner.status == 'centered'
  # Near the left/right edge, a sufficient inward path needs no additional push.
  _, _, _, _, _, tick = scenario(side*.65)
  for _ in range(60):
    assert tick(side*.004) == 0.


@pytest.mark.parametrize('fault', ['signalL', 'signalR', 'driver', 'inactive', 'low', 'reverse', 'can', 'timeout',
                                  'nan', 'hugeSpeed', 'staleCar', 'staleModel', 'quality', 'geometry', 'turn', 'laneChange', 'off'])
def test_every_abort_clears_a_built_correction(fault):
  owner, cs, md, sm, settings, tick = scenario(.65)
  for _ in range(100):
    tick()
  assert owner.accel > 0
  if fault == 'signalL':
    cs.leftBlinker = True
  elif fault == 'signalR':
    cs.rightBlinker = True
  elif fault == 'driver':
    cs.steeringPressed = True
  elif fault == 'low':
    cs.vEgo = 4.9
  elif fault == 'reverse':
    cs.gearShifter = 'reverse'
  elif fault == 'can':
    cs.canValid = False
  elif fault == 'timeout':
    cs.canTimeout = True
  elif fault == 'nan':
    cs.vEgo = math.nan
  elif fault == 'hugeSpeed':
    cs.vEgo = 1e308
  elif fault == 'staleCar':
    sm.alive['carState'] = False
  elif fault == 'staleModel':
    sm.valid['modelV2'] = False
  elif fault == 'quality':
    md.laneLineProbs[1:3] = [.1, .1]
  elif fault == 'geometry':
    md.laneLines[2].y = md.laneLines[1].y
  elif fault == 'turn':
    md.meta.desire = 'turnLeft'
  elif fault == 'laneChange':
    md.meta.laneChangeState = 'preLaneChange'
  elif fault == 'off':
    settings[0] = False
  if fault != 'off':
    assert tick(active=fault != 'inactive') == 0.
  for _ in range(30):
    result = tick(active=fault != 'inactive')
  assert result == owner.accel == owner.fraction == 0.


@pytest.mark.parametrize('field', ['x', 'y'])
@pytest.mark.parametrize('issue', ['nan', 'reverse', 'short'])
def test_invalid_lane_geometry_is_not_replaced_by_an_invented_line(field, issue):
  owner, _, md, _, _, tick = scenario(.65)
  for line in md.laneLines[1:3]:
    values = np.array(getattr(line, field))
    if issue == 'nan':
      values[3] = math.nan
    elif issue == 'reverse':
      values = values[::-1]
    else:
      values = values[:3]
    setattr(line, field, values)
  for _ in range(30):
    result = tick()
  # Reversing only y is finite but geometrically plausible for a straight line;
  # it does not manufacture a new correction direction.
  if field == 'y' and issue == 'reverse':
    assert result >= 0
  else:
    assert result == 0


def test_indicator_hold_and_frozen_frames_cannot_reapply_a_cached_push():
  owner, cs, _, _, _, tick = scenario(.65)
  for _ in range(100):
    tick()
  cs.leftBlinker = True
  assert tick() == 0
  cs.leftBlinker = False
  for _ in range(98):
    assert tick() == 0
  for _ in range(10):
    tick()
  assert owner.accel > 0
  for _ in range(22):
    tick(fresh=False)
  assert owner.accel == 0 and owner.status == 'inputs'


def test_geometry_work_is_once_per_model_frame_not_per_control_tick(monkeypatch):
  owner, _, _, _, _, tick = scenario(.65)
  calls = []
  geometry = owner._geometry
  monkeypatch.setattr(owner, '_geometry', lambda *args: calls.append(1) or geometry(*args))
  tick()
  for i in range(100):
    tick(fresh=i % 5 == 0)
  assert len(calls) == 20


@pytest.mark.parametrize('car,not_car', [('KIA_PV5', False), ('KIA_EV6', False), ('', False), ('KIA_PV5', True)])
def test_production_constructor_admits_only_actual_pv5(car, not_car):
  source = (Path(__file__).parents[1] / 'controlsd.py').read_text(encoding='utf-8')
  tree = ast.parse(source)
  block = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and 'Pv5LaneBoundary(self.params)' in ast.unparse(n))
  calls = []
  owner = NS(CP=NS(carFingerprint=car, notCar=not_car), params=None, pv5_lane_center=None)
  exec(compile(ast.Module(body=[block], type_ignores=[]), '<production vehicle scope>', 'exec'),
       {'self': owner, 'Pv5LaneBoundary': lambda _: calls.append(1)})
  assert len(calls) == int(car == 'KIA_PV5' and not not_car)
