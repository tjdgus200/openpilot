from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openpilot.cereal import car as car_schema, log
from openpilot.selfdrive.controls.lib import desire_helper
from openpilot.selfdrive.controls.lib.desire_lib import boundary_lane
from openpilot.selfdrive.modeld.constants import ModelConstants


NOW_NS = 100_000_000_000
STATE = log.LaneChangeState
SIDES = ("left", "right")
MODES = (0, 1, 2)


def _line(y):
  return SimpleNamespace(y=[y] * len(ModelConstants.T_IDXS))


@pytest.fixture
def lane_change(monkeypatch):
  clock = SimpleNamespace(CLOCK_BOOTTIME=7, clock_gettime_ns=lambda _clock_id: NOW_NS,
                          monotonic=lambda: NOW_NS / 1e9, monotonic_ns=lambda: NOW_NS)
  monkeypatch.setattr(desire_helper, "time", clock)
  monkeypatch.setattr(boundary_lane, "time", clock)
  monkeypatch.setattr(desire_helper, "Params", Mock())
  command_reader = Mock()
  command_reader.read.return_value = None
  monkeypatch.setattr(desire_helper, "CommandReader", lambda _scope: command_reader)

  def setup(mode=1, *, pv5_oem_lane_check=True, right_last_lane=False, warmup=24):
    helper = desire_helper.DesireHelper(pv5_oem_lane_check=pv5_oem_lane_check)
    if mode is None:
      helper.params.get_int.side_effect = lambda key: int(key == "LaneLineCheck")
    else:
      monkeypatch.setattr(helper, "_update_params_periodic", lambda: None)
      helper.laneLineCheck = mode
    helper.laneChangeNeedTorque = 0
    helper.laneChangeBsd = 0
    helper.laneChangeDelay = 0.0
    car = SimpleNamespace(
      canValid=True, canTimeout=False, trailerConnected=False, vEgo=20.0, aEgo=0.0,
      steeringAngleDeg=0.0, leftBlinker=False, rightBlinker=False, steeringPressed=False,
      steeringTorque=0.0, leftLaneLine=10, rightLaneLine=10, leftBlindspot=False, rightBlindspot=False,
      pv5LaneMarking=SimpleNamespace(
        monoTime=NOW_NS, leftType=0, rightType=0,
        cameraMonoTime=NOW_NS, leftCameraCode=8, rightCameraCode=8,
      ),
    )
    model = SimpleNamespace(
      laneLines=[_line(y) for y in (-5.4, -1.8, 1.8, 2.5 if right_last_lane else 5.4)],
      roadEdges=[_line(-8.0), _line(2.6 if right_last_lane else 8.0)],
      laneLineProbs=[0.99, 0.99, 0.99, 0.0 if right_last_lane else 0.99],
      orientationRate=SimpleNamespace(z=[0.0] * 16), meta=SimpleNamespace(desireState=[0.0] * 8),
    )
    radar = SimpleNamespace(leadLeft=SimpleNamespace(status=False), leadRight=SimpleNamespace(status=False),
                            leadsLeft=(), leadsRight=())
    nav = SimpleNamespace(atcType="", carrotCmdIndex=0, carrotCmd="", carrotArg="")

    def step(count=1, lane_change_prob=0.5):
      for _ in range(count):
        helper.update(car, model, True, lane_change_prob, nav, radar)

    step(warmup)
    return helper, car, model, nav, step

  return setup


@pytest.mark.parametrize("pv5_enabled", [True, False])
def test_saved_mode_one_is_loaded_on_first_pv5_model_frame(lane_change, pv5_enabled):
  helper, car, _, _, step = lane_change(None, pv5_oem_lane_check=pv5_enabled, warmup=1)
  assert helper.laneLineCheck == int(pv5_enabled)
  car.pv5LaneMarking.cameraMonoTime = 0
  step(24)
  car.leftBlinker = True
  step(12)
  assert helper.lane_change_state == (STATE.preLaneChange if pv5_enabled else STATE.laneChangeStarting)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("camera_code", [8, 0, 2, 4, 5, -1])
def test_every_pv5_mode_requires_raw_eight_on_the_target_side(lane_change, mode, side, camera_code):
  helper, car, _, _, step = lane_change(mode)
  # On a rejected sample the opposite side still has code 8 and legacy fields still show dashed.
  setattr(car.pv5LaneMarking, f"{side}CameraCode", camera_code)
  setattr(car, f"{side}Blinker", True)
  step(12)
  assert helper.lane_change_state == (STATE.laneChangeStarting if camera_code == 8 else STATE.preLaneChange)


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("stamp,allowed", [
  (NOW_NS, True),
  (NOW_NS - 250_000_000, True),
  (NOW_NS - 250_000_001, False),
  (NOW_NS + 1, False),
  (0, False),
  (None, False),
])
def test_target_code_requires_fresh_original_camera_timestamp(lane_change, side, stamp, allowed):
  helper, car, _, _, step = lane_change()
  if stamp is None:
    del car.pv5LaneMarking
  else:
    car.pv5LaneMarking.cameraMonoTime = stamp
  setattr(car, f"{side}Blinker", True)
  step(12)
  assert helper.lane_change_state == (STATE.laneChangeStarting if allowed else STATE.preLaneChange)


def test_previous_message_with_only_display_markings_has_no_camera_evidence(lane_change):
  helper, car, _, _, step = lane_change()
  recorded_state = car_schema.CarState.new_message()
  marking = recorded_state.pv5LaneMarking
  marking.monoTime = NOW_NS
  marking.leftType = 0
  marking.rightType = 0
  with car_schema.CarState.from_bytes(recorded_state.to_bytes()) as decoded:
    assert decoded.pv5LaneMarking.cameraMonoTime == 0
    assert decoded.pv5LaneMarking.leftCameraCode == -1
    assert decoded.pv5LaneMarking.rightCameraCode == -1
    car.pv5LaneMarking = decoded.pv5LaneMarking
    car.leftBlinker = True
    step(12)
    assert helper.lane_change_state == STATE.preLaneChange


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("field,value", [("canValid", False), ("canTimeout", True)])
@pytest.mark.parametrize("nav_confirmation", [False, True])
def test_invalid_can_blocks_fresh_code_eight(lane_change, side, field, value, nav_confirmation):
  helper, car, _, nav, step = lane_change()
  if nav_confirmation:
    nav.atcType = f"fork {side}"
  setattr(car, field, value)
  setattr(car, f"{side}Blinker", True)
  step(12)
  assert helper.lane_change_state != STATE.laneChangeStarting


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("camera_code", [8, 0, 2, 4])
def test_matching_navigation_and_driver_blinker_require_camera_evidence(lane_change, mode, side, camera_code):
  helper, car, _, nav, step = lane_change(mode)
  setattr(car.pv5LaneMarking, f"{side}CameraCode", camera_code)
  setattr(car, f"{side}LaneLine", 11)
  setattr(car, f"{side}Blinker", True)
  nav.atcType = f"fork {side}"
  step(12)
  assert helper.lane_change_state == (STATE.laneChangeStarting if camera_code == 8 else STATE.preLaneChange)


@pytest.mark.parametrize("side", SIDES)
def test_bluetooth_request_cannot_substitute_for_camera_evidence(lane_change, side):
  helper, car, _, nav, step = lane_change()
  setattr(car.pv5LaneMarking, f"{side}CameraCode", 2)
  setattr(car, f"{side}Blinker", True)
  opposite = "right" if side == "left" else "left"
  nav.atcType = f"fork {opposite}"
  helper.bluetooth_commands.read.return_value = f"lane{side.title()}"
  step(12)
  assert helper.lane_change_state == STATE.preLaneChange


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("camera_code", [8, 0, 2, 4, -1])
def test_nav_only_appearing_right_lane_requires_camera_evidence(lane_change, mode, camera_code):
  helper, car, model, nav, step = lane_change(mode, right_last_lane=True)
  car.pv5LaneMarking.rightCameraCode = camera_code
  nav.atcType = "fork right"
  step(3)
  assert helper.lane_change_state == STATE.preLaneChange
  assert helper.auto_lane_change_enable

  model.laneLines[3] = _line(5.4)
  model.roadEdges[1] = _line(5.2)
  model.laneLineProbs[3] = 0.99
  step(24)
  assert helper.right.lane_available and helper.right.edge_available
  assert helper.lane_change_state == (STATE.laneChangeStarting if camera_code == 8 else STATE.preLaneChange)


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("camera_code", [8, 2])
def test_torque_triggered_mode_two_assist_requires_camera_evidence(lane_change, side, camera_code):
  helper, car, _, _, step = lane_change(2)
  helper.laneChangeNeedTorque = 1
  setattr(car.pv5LaneMarking, f"{side}CameraCode", camera_code)
  setattr(car, f"{side}LaneLine", 11)
  setattr(car, f"{side}Blinker", True)
  step(3)
  assert helper.lane_change_state == STATE.preLaneChange
  car.steeringPressed = True
  car.steeringTorque = 1.0 if side == "left" else -1.0
  step(3)
  assert helper.lane_change_state == (STATE.laneChangeStarting if camera_code == 8 else STATE.preLaneChange)


@pytest.mark.parametrize("side", SIDES)
def test_reacquired_dashed_marking_does_not_skip_driver_delay(lane_change, side):
  helper, car, _, _, step = lane_change()
  helper.laneChangeDelay = 1.0
  setattr(car, f"{side}Blinker", True)
  step(3)
  assert helper.lane_change_state == STATE.preLaneChange
  assert helper.lane_change_delay > 0.0

  car.pv5LaneMarking.cameraMonoTime = NOW_NS - 250_000_001
  step(2)
  car.pv5LaneMarking.cameraMonoTime = NOW_NS
  step(2)
  assert helper.lane_change_delay > 0.0
  assert helper.lane_change_state == STATE.preLaneChange

  step(20)
  assert helper.lane_change_delay == 0.0
  assert helper.lane_change_state == STATE.laneChangeStarting


@pytest.mark.parametrize("side", SIDES)
def test_marking_loss_blocks_next_start_without_cancelling_in_progress(lane_change, side):
  helper, car, _, _, step = lane_change()
  setattr(car, f"{side}Blinker", True)
  step(3)
  assert helper.lane_change_state == STATE.laneChangeStarting
  direction = helper.lane_change_direction

  setattr(car.pv5LaneMarking, f"{side}CameraCode", 4)
  step(3)
  assert helper.lane_change_state == STATE.laneChangeStarting
  assert helper.lane_change_direction == direction

  step(12, lane_change_prob=0.0)
  assert helper.lane_change_state == STATE.laneChangeFinishing
  step(24, lane_change_prob=0.0)
  assert helper.lane_change_state == STATE.preLaneChange

  car.steeringPressed = True
  car.steeringTorque = 1.0 if side == "left" else -1.0
  step(3)
  assert helper.lane_change_state == STATE.preLaneChange
  setattr(car.pv5LaneMarking, f"{side}CameraCode", 8)
  step(3)
  assert helper.lane_change_state == STATE.laneChangeStarting


@pytest.mark.parametrize("mode,legacy_line,camera_code,torque,allowed", [
  (0, 11, -1, False, True),
  (0, 21, 8, False, False),
  (1, 10, -1, False, True),
  (1, 11, 8, False, False),
  (2, 11, 8, False, False),
  (2, 11, -1, True, True),
])
def test_other_vehicles_keep_legacy_behavior(lane_change, mode, legacy_line, camera_code, torque, allowed):
  helper, car, _, _, step = lane_change(mode, pv5_oem_lane_check=False)
  car.pv5LaneMarking.leftCameraCode = camera_code
  car.leftLaneLine = legacy_line
  car.leftBlinker = True
  car.steeringPressed = torque
  car.steeringTorque = 1.0 if torque else 0.0
  step(12)
  assert helper.lane_change_state == (STATE.laneChangeStarting if allowed else STATE.preLaneChange)
