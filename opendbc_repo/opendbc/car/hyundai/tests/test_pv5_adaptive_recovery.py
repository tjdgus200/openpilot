"""Authority-only feedback recovery; these tests do not model a physical EPS."""
import math

import pytest

from opendbc.car.hyundai.tests.test_steering_recovery import Trace


CUES = {'target_error': 12., 'command_error': 12., 'speed': 15., 'wheelbase': 3., 'steer_ratio': 14.26}


@pytest.mark.parametrize('direction', (-1, 1))
@pytest.mark.parametrize('release_ms', (100, 200, 500))
@pytest.mark.parametrize('recovery_ms', (1000, 1200, 1500, 3000))
def test_adaptive_rise_keeps_confirmation_and_all_existing_rate_ceilings(direction, release_ms, recovery_ms):
  trace = Trace()
  cues = CUES | {'target_error': direction * 12., 'command_error': direction * 12.,
                 'release_ms': release_ms, 'recovery_ms': recovery_ms}
  assert trace.step(**cues) == 25
  for _ in range(release_ms // 10 - 1):
    assert trace.step(**cues) == 25
  base_step = 225 * .01 / (recovery_ms / 1000)
  previous = trace.previous
  observed = []
  for _ in range(450):
    cap = trace.step(**cues)
    rise = cap - previous
    assert 0 <= rise <= min(2.25, base_step * 1.5) + 1e-9
    assert 25 <= cap <= 250
    observed.append(rise)
    previous = cap
  assert cap == 250
  assert max(observed) == pytest.approx(min(2.25, base_step * 1.5))


@pytest.mark.parametrize('error', (0., .1, 1., 3.))
def test_small_error_uses_exact_configured_base_rate(error):
  baseline, adaptive = Trace(), Trace()
  for _ in range(200):
    assert adaptive.step(**(CUES | {'target_error': error, 'command_error': error})) == baseline.step()


@pytest.mark.parametrize('bad_cues', (
  {'target_error': -12}, {'command_error': 0}, {'command_error': -12},
  {'command_error': .2}, {'speed': 0}, {'speed': -1}, {'wheelbase': 0}, {'steer_ratio': 0},
  *({key: value} for key in CUES for value in (math.nan, math.inf, -math.inf)),
))
def test_bad_or_disagreeing_cues_cannot_raise_recovery_above_baseline(bad_cues):
  baseline, adaptive = Trace(), Trace()
  for _ in range(170):
    assert adaptive.step(**(CUES | bad_cues)) == baseline.step()
  assert adaptive.helper.recovery_boost == 0


def test_both_errors_are_needed_and_unlimited_request_cannot_bypass_command_limit():
  limited, huge_request = Trace(), Trace()
  for _ in range(170):
    a = limited.step(**(CUES | {'target_error': 4., 'command_error': 4.}))
    b = huge_request.step(**(CUES | {'target_error': 175., 'command_error': 4.}))
    assert a == b


def test_sustained_error_qualification_slew_noise_and_sign_reversal():
  trace = Trace()
  trace.step(**CUES)
  previous_boost = 0.
  for index in range(35):
    trace.step(**CUES)
    boost = trace.helper.recovery_boost
    assert 0 <= boost - previous_boost <= .05 + 1e-9
    if index < 9:
      assert boost == 0
    previous_boost = boost
  assert boost == 1
  # Falling demand decays smoothly; it cannot keep a completed ramp accelerated.
  previous = trace.helper.recovery_boost
  trace.step(**(CUES | {'target_error': 1, 'command_error': 1}))
  assert trace.helper.recovery_boost == pytest.approx(previous - .05)
  trace.step(**(CUES | {'target_error': -12, 'command_error': -12}))
  assert trace.helper.recovery_boost == 0
  assert trace.helper.error_seconds == pytest.approx(.01)
  for _ in range(50):
    trace.step(**(CUES | {'target_error': 12 if _ % 2 else -12, 'command_error': 12 if _ % 2 else -12}))
    assert trace.helper.recovery_boost == 0


@pytest.mark.parametrize('event', (
  {'pressed': True}, {'pre_override_yield': .5}, {'driver_ratio': .7},
  {'valid': False}, {'active': False, 'legacy': 0}, {'enabled': False},
))
def test_driver_fault_deactivation_and_switch_off_clear_acceleration_immediately(event):
  trace = Trace()
  for _ in range(45):
    trace.step(**CUES)
  assert trace.helper.recovery_boost > 0
  previous = trace.previous
  cap = trace.step(**(CUES | event))
  assert trace.helper.recovery_boost == 0
  if event.get('driver_ratio') == .7:
    assert cap == pytest.approx(previous + .75)
  elif event.get('enabled') is False:
    assert cap == previous
    assert trace.step(**(CUES | event)) == pytest.approx(previous + 1.5)
  else:
    assert cap <= previous


@pytest.mark.parametrize('gap', (.1, -1., math.nan))
def test_invalid_clock_clears_existing_boost_and_requalifies(gap):
  trace = Trace()
  for _ in range(45):
    trace.step(**CUES)
  trace.now += gap
  assert trace.step(**CUES) <= 25
  assert trace.helper.recovery_boost == 0
  assert not trace.helper.released


def test_fully_recovered_output_skips_error_math():
  trace = Trace(250)
  trace.step(**CUES)
  def unexpected(*args):
    pytest.fail('no adaptive work is needed at full authority')
  trace.helper._update_boost = unexpected
  for _ in range(30):
    assert trace.step(**CUES) == 250


def test_speed_normalization_does_not_treat_low_speed_angle_as_high_speed_error():
  boosts = []
  for speed in (5., 15., 30.):
    trace = Trace()
    for _ in range(32):
      trace.step(**(CUES | {'target_error': 4., 'command_error': 4., 'speed': speed}))
    boosts.append(trace.helper.recovery_boost)
  assert boosts[0] == 0
  assert 0 < boosts[1] < boosts[2] <= 1
