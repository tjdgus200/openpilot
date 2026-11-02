"""PC regressions for TBT completion, cancellation and retained cue ownership."""
import math

import pytest

from test_turn_wheel_return import turn


@pytest.mark.parametrize('direction,sign', [('left', 1), ('right', -1)])
def test_return_waits_through_stop_and_releases_at_one_kmh(direction, sign):
  journey, step = turn(direction)
  assert step(12., angle=90 * sign, heading=-.5 * sign)
  for now, speed in ((13., 0.), (14., 1 / 3.6 - .00001)):
    assert step(now, angle=30 * sign, heading=-.5 * sign, speed=speed)
  assert step(15., angle=30 * sign, heading=-.5 * sign, speed=1 / 3.6) is None
  assert journey.reason == 'completed'


@pytest.mark.parametrize('direction,sign', [('left', 1), ('right', -1)])
def test_return_band_is_absolute_and_does_not_require_new_heading_motion(direction, sign):
  journey, step = turn(direction)
  assert step(12., angle=90 * sign, heading=-.5 * sign)
  assert step(13., angle=-30.001 * sign, heading=-.5 * sign)
  assert step(14., angle=-30 * sign, heading=-.5 * sign) is None
  assert journey.reason == 'completed'


@pytest.mark.parametrize('field', ['speed', 'angle', 'heading', 'torque', 'cue'])
@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_nonfinite_return_input_clears_current_and_pending_without_replay(field, bad):
  journey, step = turn()
  assert step(12., angle=90., heading=-.5)
  assert step(13., cue=13., distance=80., angle=80., heading=-.6)
  assert journey.pending
  args = {'angle': 25., 'heading': -.6, field: bad}
  assert step(14., **args) is None
  assert journey.active is None and journey.pending is None
  # The original cue is too old to admit a new turn after invalid input.
  assert step(15.) is None


@pytest.mark.parametrize('fault', ['valid', 'cancelled', 'epoch', 'missing_travel', 'route'])
def test_revocation_precedes_wheel_return_and_discards_next_turn(fault):
  journey, step = turn()
  journey.active['target'] = (1., 2., 'route-a')
  assert step(12., angle=90., heading=-.5)
  assert step(13., cue=13., distance=80., angle=80., heading=-.6)
  assert journey.pending
  args = dict(angle=25., heading=-.6)
  args.update({'valid': {'valid': False}, 'cancelled': {'cancelled': True},
               'epoch': {'travel': (1, 0.)}, 'missing_travel': {'travel': None},
               'route': {'target': (1., 2., 'route-b')}}[fault])
  assert step(14., **args) is None
  assert journey.active is None and journey.pending is None
  assert journey.reason != 'completed'


def test_same_target_repeated_audio_does_not_queue_or_extend_hold():
  journey, step = turn()
  target = (1., 2., 'route-a')
  journey.active['target'] = target
  assert step(12., angle=90., heading=-.5)
  assert step(13., cue=13., distance=200., target=target, angle=80., heading=-.6)
  assert journey.pending is None and journey.active['end'] == 70.
  assert step(14., angle=25., heading=-.6) is None
  assert step(14.1, angle=25., heading=-.6) is None


@pytest.mark.parametrize('direction', ['left', 'right'])
def test_distance_fallback_still_ends_unentered_turn(direction):
  journey, step = turn(direction)
  assert step(12., travel=(0, 69.))
  assert step(13., travel=(0, 70.)) is None
  assert journey.reason == 'completed'
