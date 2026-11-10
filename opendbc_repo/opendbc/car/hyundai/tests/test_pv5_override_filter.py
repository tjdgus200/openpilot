"""Exercise the real controller and CAN output; driver yielding is never delayed."""
import pytest

from opendbc.car.hyundai.tests.test_steering_handover_controller import setup_controller, step
from opendbc.car.hyundai.values import CAR


def setup(monkeypatch, pv5=True, angle=True):
  controller, cs, cc, settings = setup_controller(monkeypatch, angle=angle)
  controller.CP.brand = "hyundai"
  controller.CP.carFingerprint = CAR.KIA_PV5 if pv5 else CAR.KIA_EV6
  cs.out.steeringPressed, cs.out.steeringTorque = False, 0
  return controller, cs, cc, settings


def press(controller, cs, cc, count):
  cs.out.steeringPressed, cs.out.steeringTorque = True, 300
  for _ in range(count):
    step(controller, cs, cc)


@pytest.mark.parametrize("frames", (1, 2, 20, 29, 30, 31, 60))
def test_only_sustained_press_adds_recovery_penalty(monkeypatch, frames):
  controller, cs, cc, _ = setup(monkeypatch)
  controller.full_recovery_frames = 100
  press(controller, cs, cc, frames)
  assert controller.repeated_override_count == int(frames >= 30)
  assert controller.recovering_from_override
  # Immediate physical driver yielding is preserved even on the first tick.
  assert controller.lkas_max_torque == controller.params.ANGLE_MIN_TORQUE


def test_interrupted_short_presses_never_accumulate(monkeypatch):
  controller, cs, cc, _ = setup(monkeypatch)
  for _ in range(5):
    controller.full_recovery_frames = 100
    press(controller, cs, cc, 29)
    cs.out.steeringPressed, cs.out.steeringTorque = False, 0
    step(controller, cs, cc)
  assert controller.repeated_override_count == 0
  controller.full_recovery_frames = 100
  press(controller, cs, cc, 30)
  assert controller.repeated_override_count == 1


def test_release_inactive_and_new_engagement_reset_confirmation(monkeypatch):
  controller, cs, cc, _ = setup(monkeypatch)
  controller.full_recovery_frames = 100
  press(controller, cs, cc, 29)
  cc.latActive = False
  output, _ = step(controller, cs, cc)
  assert output.torqueOutputCan == 0
  assert controller.steering_pressed_frames == controller.repeated_override_count == 0
  cc.latActive = True
  press(controller, cs, cc, 1)
  assert controller.steering_pressed_frames == 1
  assert controller.repeated_override_count == 0


@pytest.mark.parametrize("pv5,angle", ((False, True), (False, False), (True, False)))
def test_other_cars_and_torque_control_keep_edge_counting(monkeypatch, pv5, angle):
  controller, cs, cc, _ = setup(monkeypatch, pv5=pv5, angle=angle)
  controller.full_recovery_frames = 100
  press(controller, cs, cc, 1)
  assert controller.repeated_override_count == 1
  assert controller.steering_pressed_frames == 0
