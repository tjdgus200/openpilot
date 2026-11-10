"""Bilateral published-geometry fit; real controls admission is tested separately."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.controls.lib.pv5_lane_center import MAX_CORRECTION_ACCEL, _target_accel


def frame(*, curvature=0., offset=0., probs=(1., 1.), stds=(.1, .1), speed=20.):
  look = max(max(speed, 5.) * 1.5, 10.)
  xs = [0.] + [look * (.5 + i / 8) for i in range(9)] + [2 * look]
  path = [curvature * x*x / 2 for x in xs]
  def line(ys):
    return NS(x=xs[:], y=ys)
  return NS(position=line(path), laneLines=[line(path), line([y + offset - 1.7 for y in path]),
            line([y + offset + 1.7 for y in path]), line(path)],
            laneLineProbs=[0., *probs, 0.], laneLineStds=[0., *stds, 0.], leadsV3=[NS(prob=0.)])


@pytest.mark.parametrize('curve', [-.006, -.002, 0., .002, .006])
def test_coherent_curved_road_does_not_add_a_steering_bias(curve):
  value, reason, trust = _target_accel(frame(curvature=curve), 20., curve)
  assert value == 0. and reason == 'centered' and trust == 1.


@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('curve', [-.002, 0., .002])
def test_constant_center_offset_matches_independent_least_squares(curve, direction):
  # An offset-only residual has the closed form 2*e*sum(x^2)/sum(x^4).
  offset = direction * .025
  md = frame(curvature=curve, offset=offset)
  xs = md.position.x[1:-1]
  expected = 2 * offset * sum(x*x for x in xs) / sum(x**4 for x in xs) * 400
  value, reason, trust = _target_accel(md, 20., curve)
  assert value == pytest.approx(expected, abs=1e-12)
  assert reason == 'checking' and trust == 1.


@pytest.mark.parametrize('direction', [-1, 1])
def test_fit_detects_action_curvature_error_when_model_path_is_centered(direction):
  curve, action = direction * .0021, direction * .002
  value, _, _ = _target_accel(frame(curvature=curve), 20., action)
  # At this curve the action/plan mix is 64.666...% / 35.333...%.
  assert value == pytest.approx((curve-action) * (1 - (0.3 + 0.2 * (.9-.5)/1.5)) * 400)


@pytest.mark.parametrize('prob', [.5, .7, .84, .85, 1.])
@pytest.mark.parametrize('std', [0., .1, .2, .4, .59])
def test_quality_is_continuous_and_both_lines_contribute(prob, std):
  target, reason, quality = _target_accel(frame(offset=.27, probs=(1., prob), stds=(.1, std)), 20., 0.)
  assert 0 < target <= MAX_CORRECTION_ACCEL and reason == 'checking'
  assert quality == pytest.approx(prob * min(1., (.6-std)/.4))


@pytest.mark.parametrize('prob,std', [(0., .1), (.49, .1), (1.01, .1), (math.nan, .1),
                                     (.99, -.01), (.99, .6), (.99, math.inf)])
def test_no_single_line_or_invalid_quality_fallback(prob, std):
  assert _target_accel(frame(offset=.1, probs=(1., prob), stds=(.1, std)), 20., 0.) == (0., 'laneQuality', 0.)


@pytest.mark.parametrize('line', [0, 1, 2])
@pytest.mark.parametrize('fault', ['nan_tail', 'duplicate', 'reversed', 'short', 'too_many', 'coverage'])
def test_invalid_or_uncovered_geometry_is_not_extrapolated(line, fault):
  md = frame(offset=.1)
  target = [md.laneLines[1], md.laneLines[2], md.position][line]
  if fault == 'nan_tail':
    target.y[-1] = math.nan
  elif fault == 'duplicate':
    target.x[2] = target.x[1]
  elif fault == 'reversed':
    target.x.reverse()
  elif fault == 'short':
    target.y.pop()
  elif fault == 'too_many':
    target.x = list(map(float, range(34)))
    target.y = [0.]*34
  else:
    target.x = [x / 10 for x in target.x]
  assert _target_accel(md, 20., 0.) == (0., 'geometry', 0.)


@pytest.mark.parametrize('speed', [0., -1., math.nan, math.inf, 1000.])
def test_invalid_speed_fails_closed(speed):
  assert _target_accel(frame(offset=.1), speed, 0.) == (0., 'geometry', 0.)


@pytest.mark.parametrize('curvature', [math.nan, math.inf, -math.inf, 1e308, -1e308])
def test_invalid_or_overflowing_curvature_fails_closed(curvature):
  assert _target_accel(frame(offset=.1), 20., curvature) == (0., 'geometry', 0.)


def admitted():
  from openpilot.selfdrive.controls.lib.pv5_lane_center import Pv5LaneCenter
  model = frame(offset=.1)
  model.frameId = 0
  model.action = NS(desiredCurvature=0.)
  model.meta = NS(laneChangeState='off', desire='none', leverTurn=False,
                  autoTurnSignal='none', hardBrakePredicted=False)
  cs = NS(canValid=True, canTimeout=False, gearShifter='drive', standstill=False,
          vEgo=20., leftBlindspot=False, rightBlindspot=False, steeringPressed=False,
          brakePressed=False, leftBlinker=False, rightBlinker=False)
  sm = type('Messages', (dict,), {})(modelV2=model, carState=cs, lateralPlan=NS(), selfdriveState=NS(),
       radarState=NS(radarErrors=NS(canError=False, radarFault=False, wrongConfig=False,
                                  radarUnavailableTemporary=False), leadOne=NS(status=False), leadTwo=NS(status=False)),
       driverMonitoringState=NS(lockout=False, alertLevel='none', activePolicy='off'))
  sm.seen = sm.valid = sm.alive = dict.fromkeys(sm, True)
  sm.recv_frame = {'modelV2': 0}
  assist = Pv5LaneCenter(NS(get_bool=lambda _: True))
  def tick(now):
    model.frameId += 1
    sm.recv_frame['modelV2'] += 1
    sm.recv_time = dict.fromkeys(sm, now)
    return assist.update(sm, cs, model, now, active=True, lane_mode=False)
  return assist, model, cs, tick


@pytest.mark.parametrize('gap', [0., -.05, .25])
def test_clock_discontinuity_clears_trust_and_restarts_dwell(gap):
  assist, _, _, tick = admitted()
  for i in range(50):
    tick(100.+i*.05)
  assert assist.target > 0
  assert tick(102.45+gap) == 0.
  assert assist.target == assist.trust == 0 and assist.eligible_since is None
  assert assist.status == 'inputs'


def test_quality_drop_retains_dwell_until_either_line_is_missing():
  assist, model, _, tick = admitted()
  for i in range(50):
    tick(100.+i*.05)
  before, since = assist.target, assist.eligible_since
  model.laneLineProbs[2] = .84
  assert tick(102.5) > 0 and assist.eligible_since == since
  assert assist.target < before
  model.laneLineProbs[2] = .49
  assert tick(102.55) == 0 and assist.trust == 0 and assist.status == 'laneQuality'


@pytest.mark.parametrize('speed', [1e-8, 1., 5., 20., 55.])
def test_finite_low_speed_bound_and_bilateral_quality_ceiling(speed):
  md = frame(offset=.27, speed=speed, probs=(1., .5))
  accel, _, quality = _target_accel(md, speed, 0.)
  curvature = accel * quality / max(speed, 5.)**2
  assert math.isfinite(curvature) and 0 <= curvature * speed**2 <= .04 + 1e-12
