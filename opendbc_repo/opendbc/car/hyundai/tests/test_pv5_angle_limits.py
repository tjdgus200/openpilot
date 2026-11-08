from types import SimpleNamespace as NS

import pytest

from opendbc.car.hyundai.carcontroller import angle_comfort_rate_limit, apply_steer_angle_limits_physics


def model(value):
  return NS(position=NS(yStd=[value] * 33))


@pytest.mark.parametrize('speed,expected', ((0, .8), (15 / 3.6, 1.1), (40 / 3.6, 2.0)))
def test_independent_low_speed_and_uncertainty_budgets(speed, expected):
  assert angle_comfort_rate_limit(5, 0, speed, model(.1)) == pytest.approx(expected)
  assert angle_comfort_rate_limit(5, 0, speed, model(.4)) == .8


def test_large_error_and_directional_unwind_keep_their_original_bounds():
  assert angle_comfort_rate_limit(100, 0, 20, model(.1)) == 1.0
  assert angle_comfort_rate_limit(120, 90, 0, model(.4)) == .8
  assert angle_comfort_rate_limit(0, 90, 0, model(.4)) == 1.5
  assert angle_comfort_rate_limit(0, -90, 0, model(.4)) == 1.5


@pytest.mark.parametrize('active', (True, False))
@pytest.mark.parametrize('speed', (0, 5, 15, 30, 50))
@pytest.mark.parametrize('sign', (-1, 1))
def test_absolute_cap_and_speed_physics_are_still_enforced(active, speed, sign):
  output = apply_steer_angle_limits_physics(sign * 500, sign * 174, speed, sign * 200,
                                           active, 2.995, 16, 175, model(.1))
  assert abs(output) <= 175
  if not active:
    assert output == sign * 175
  elif speed >= 30:
    assert abs(output) < 30
