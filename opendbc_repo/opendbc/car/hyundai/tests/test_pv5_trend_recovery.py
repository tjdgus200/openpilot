"""Trend shaping is bounded authority recovery, not a physical EPS guarantee."""
import math

import pytest

from opendbc.car.hyundai.tests.test_pv5_adaptive_recovery import CUES
from opendbc.car.hyundai.tests.test_steering_recovery import Trace


def pair():
  level, trend = Trace(), Trace()
  level.helper._trend_demand = lambda dt, errors, margin, demand: demand
  return level, trend


@pytest.mark.parametrize('direction', (-1, 1))
@pytest.mark.parametrize('slope', (-.05, 0., .02))
def test_converging_constant_and_diverging_errors(direction, slope):
  level, trend = pair()
  start = 12. if slope < 0 else 4.
  differences = []
  for tick in range(100):
    error = direction * (start + tick * slope)
    cues = CUES | {'target_error': error, 'command_error': error}
    previous = trend.previous
    a, b = level.step(**cues), trend.step(**cues)
    assert 0 <= b - previous <= 2.25 + 1e-9
    assert 25 <= b <= 250
    differences.append(b - a)
  if slope > 0:
    assert min(differences) >= -1e-9 and max(differences) > .01
  elif slope < 0:
    assert max(differences) <= 1e-9 and min(differences) < -.01
  else:
    assert differences == [0.] * len(differences)


@pytest.mark.parametrize('direction', (-1, 1))
def test_disagreeing_trends_and_minimum_cue_switch_cannot_predict(direction):
  level, trend = pair()
  for tick in range(100):
    cues = CUES | {'target_error': direction * (4 + .04 * tick),
                   'command_error': direction * (8 - .04 * tick)}
    assert trend.step(**cues) == level.step(**cues)


@pytest.mark.parametrize('amplitude', (0., .001, .01))
def test_small_alternating_noise_does_not_change_output(amplitude):
  level, trend = pair()
  for tick in range(150):
    error = 5 + amplitude * (-1) ** tick
    cues = CUES | {'target_error': error, 'command_error': error}
    assert trend.step(**cues) == level.step(**cues)


def test_subthreshold_trend_never_qualifies_extra_acceleration():
  level, trend = pair()
  for tick in range(80):
    cues = CUES | {'target_error': .1 + .02 * tick, 'command_error': .1 + .02 * tick}
    assert trend.step(**cues) == level.step(**cues)
    assert trend.helper.recovery_boost == 0


@pytest.mark.parametrize('event', (
  {'pressed': True}, {'pre_override_yield': .1}, {'driver_ratio': .6},
  {'valid': False}, {'active': False, 'legacy': 0}, {'enabled': False},
  {'target_error': -12}, {'command_error': math.nan}, {'speed': 0},
))
def test_trend_history_is_cleared_on_every_invalid_or_override_boundary(event):
  trace = Trace()
  for tick in range(45):
    error = 4 + tick * .08
    trace.step(**(CUES | {'target_error': error, 'command_error': error}))
  assert any(abs(value) > 0 for value in trace.helper.error_rates)
  previous = trace.previous
  cap = trace.step(**(CUES | event))
  assert trace.helper.error_rates == (0., 0.)
  assert trace.helper.previous_errors is None
  if event.get('pressed') or event.get('pre_override_yield') or event.get('valid') is False:
    assert cap <= previous


def test_extreme_finite_errors_do_not_leak_nonfinite_trend():
  trace = Trace()
  for value in (4., 1e308, 4., 1e308, 5.) * 10:
    cap = trace.step(**(CUES | {'target_error': value, 'command_error': value}))
    assert math.isfinite(cap)
    assert all(math.isfinite(rate) for rate in trace.helper.error_rates)
    assert 0 <= trace.helper.recovery_boost <= 1
