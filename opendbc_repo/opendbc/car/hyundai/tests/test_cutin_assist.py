from types import SimpleNamespace as NS

import pytest

from opendbc.can import CANPacker
from opendbc.car import structs, Bus
from opendbc.car.hyundai import carcontroller
from opendbc.car.hyundai.hyundaicanfd import create_acc_control_scc2
from opendbc.car.hyundai.stopping import CanfdStopping
from opendbc.car.hyundai.values import CAR, HyundaiFlags
from opendbc.car.hyundai.tests.test_scc_decel import admission_fixture, packet_fixture, decoded_packet, T


def controller(monkeypatch, enabled=True, fingerprint=CAR.KIA_PV5):
  reads = []

  def get_bool(key):
    reads.append(key)
    return bool(enabled and key == "Pv5CutInAssist")

  monkeypatch.setattr(carcontroller, "Params", lambda: NS(get_bool=get_bool, get_int=lambda key: 0, get_float=lambda key: 0.0))
  cp = structs.CarParams(
    brand="hyundai",
    carFingerprint=fingerprint,
    notCar=False,
    flags=int(HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC),
    openpilotLongitudinalControl=True,
    safetyConfigs=[{"safetyModel": "hyundaiCanfd", "safetyParam": 45}],
  )
  return carcontroller.CarController({Bus.pt: "hyundai_canfd_generated"}, cp), reads


def inputs(stage=1):
  cc, cs = admission_fixture()
  cc.actuators = NS(longControlState=structs.CarControl.Actuators.LongControlState.pid)
  cs.out.carrotCruise = 0
  cs.radarState = NS(mdMonoTime=T, carStateMonoTime=T, pv5CutInAssistStage=stage, leadCutInAssist=NS(status=True, radar=True, dRel=8.0, vRel=-3.0, score=0.7))
  return cc, cs


def test_disabled_and_other_car_do_not_read_sources(monkeypatch):
  cc, _ = controller(monkeypatch, enabled=False)
  assert cc._apply_cutin_assist(None, None, T, 0.5) == (0.5, True)
  cc, reads = controller(monkeypatch, fingerprint=CAR.KIA_EV6)
  assert "Pv5CutInAssist" not in reads
  assert cc._apply_cutin_assist(None, None, T, 0.5) == (0.5, True)


@pytest.mark.parametrize("stage", [1, 2])
def test_actual_controller_caps_only_when_admitted(monkeypatch, stage):
  ctrl, _ = controller(monkeypatch)
  cc, cs = inputs(stage)
  accel, allowed = ctrl._apply_cutin_assist(cc, cs, T, 0.5)
  assert allowed and accel <= 0 and ctrl.cutin_assist_decision.stage == stage
  assert ctrl._apply_cutin_assist(cc, cs, T, -2.0)[0] == -2.0
  cs.out.brakePressed = True
  assert ctrl._apply_cutin_assist(cc, cs, T, 0.5) == (0.0, False)
  assert ctrl.cutin_assist_decision.stage == 0


@pytest.mark.parametrize(
  "field,value", [("gasPressed", True), ("parkingBrake", True), ("canValid", False), ("canTimeout", True), ("brakeHoldActive", True), ("gearShifter", "park")]
)
def test_driver_and_invalid_state_never_leave_assist_active(monkeypatch, field, value):
  ctrl, _ = controller(monkeypatch)
  cc, cs = inputs(2)
  setattr(cs.out, field, value)
  assert ctrl._apply_cutin_assist(cc, cs, T, 0.5) == (0.0, False)
  assert ctrl.cutin_assist_decision.stage == 0


def test_control_off_override_stale_and_nonfinite_clear(monkeypatch):
  ctrl, _ = controller(monkeypatch)
  cc, cs = inputs(2)
  cc.longActive = False
  assert ctrl._apply_cutin_assist(cc, cs, T, 0.5) == (0.0, False)
  cc.longActive = True
  cc.cruiseControl.override = True
  assert ctrl._apply_cutin_assist(cc, cs, T, 0.5) == (0.0, False)
  cc.cruiseControl.override = False
  cs.radarState.mdMonoTime = T - 150_000_001
  assert ctrl._apply_cutin_assist(cc, cs, T, 0.5) == (0.5, True)
  assert ctrl.cutin_assist_decision.stage == 0
  assert ctrl._apply_cutin_assist(cc, cs, T, float("nan")) == (0.0, False)


@pytest.mark.parametrize("ceiling", [0.0, -0.4, -0.65])
def test_final_CAN_caps_raw_without_bypassing_value_jerk(ceiling):
  packer = CANPacker("hyundai_canfd_generated")
  cs, hud, jerk = packet_fixture()
  msg, last = create_acc_control_scc2(packer, NS(ECAN=0), True, 0.5, ceiling, False, False, 40.0, hud, jerk, cs, CanfdStopping(), cutin_accel_ceiling=ceiling)
  d = decoded_packet(packer, msg)
  assert d["aReqRaw"] == pytest.approx(ceiling)
  assert d["aReqValue"] == pytest.approx(max(ceiling, .5 - jerk.jerk_l * .02))
  previous = last
  msg, last = create_acc_control_scc2(packer, NS(ECAN=0), True, last, ceiling, False, False, 40.0, hud, jerk, cs, CanfdStopping(), cutin_accel_ceiling=ceiling)
  assert last == pytest.approx(max(ceiling, previous - jerk.jerk_l * 0.02))


def test_inactive_or_other_car_packet_cannot_be_given_negative_raw_by_new_argument():
  cs, hud, jerk = packet_fixture()
  args = (NS(ECAN=0), False, 0.5, 0.0, False, False, 40.0, hud, jerk, cs)
  packer = CANPacker("hyundai_canfd_generated")
  msg, _ = create_acc_control_scc2(packer, *args, CanfdStopping(), cutin_accel_ceiling=-0.4)
  d = decoded_packet(packer, msg)
  assert d["ACCMode"] == d["aReqRaw"] == d["aReqValue"] == 0
  cs.CP.carFingerprint = CAR.KIA_EV6
  a = create_acc_control_scc2(CANPacker("hyundai_canfd_generated"), NS(ECAN=0), True, 0.5, 0.5, False, False, 40.0, hud, jerk, cs, CanfdStopping())
  b = create_acc_control_scc2(
    CANPacker("hyundai_canfd_generated"), NS(ECAN=0), True, 0.5, 0.5, False, False, 40.0, hud, jerk, cs, CanfdStopping(), cutin_accel_ceiling=-0.4
  )
  assert a == b
