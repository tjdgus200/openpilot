from types import SimpleNamespace as NS
import json
from pathlib import Path

import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car.hyundai.hyundaicanfd import hkg_can_fd_checksum, create_acc_control_scc2
from opendbc.car.hyundai.scc_decel import (
  CameraSccSource,
  Observation,
  StockDecelAssist,
  eligible,
  packet_allowed,
  feedback_active,
  warning_active,
  SOURCE_MAX_AGE_NS,
)
from opendbc.car.hyundai.stopping import CanfdStopping
from opendbc.car.hyundai.values import CAR, HyundaiFlags

T = 10_000_000_000
STEP = 20_000_000


def cp(**changes):
  return NS(
    brand="hyundai", carFingerprint=CAR.KIA_PV5, notCar=False, openpilotLongitudinalControl=True, flags=HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC, **changes
  )


def observation(t=T, raw=-2.24, value=-0.4, valid=True):
  return Observation(t, raw, value, valid)


def parser_fixture():
  return CANPacker("hyundai_canfd_generated"), CANParser("hyundai_canfd_generated", [("SCC_CONTROL", 50)], 2)


def receive(packer, parser, counter, stamp, **changes):
  values = {"COUNTER": counter, "MainMode_ACC": 1, "ACCMode": 1, "SysFailState": 0, "aReqRaw": -2.24, "aReqValue": -0.4, **changes}
  msg = packer.make_can_msg("SCC_CONTROL", 2, values)
  parser.update([(stamp, [msg])])
  return msg


def primed_source():
  packer, parser = parser_fixture()
  source = CameraSccSource(hkg_can_fd_checksum)
  receive(packer, parser, 1, T)
  assert not source.update(parser, 2, T).valid
  receive(packer, parser, 2, T + STEP)
  assert source.update(parser, 2, T + STEP).valid
  return packer, parser, source


@pytest.mark.parametrize(
  "changes",
  [
    {"brand": "toyota"},
    {"carFingerprint": CAR.KIA_EV6},
    {"notCar": True},
    {"openpilotLongitudinalControl": False},
    {"flags": HyundaiFlags.CANFD},
    {"flags": HyundaiFlags.CAMERA_SCC},
    {"flags": 0},
  ],
)
def test_scope_rejects_every_unsupported_boundary(changes):
  data = vars(cp()) | changes
  assert not eligible(NS(**data))


def test_scope_accepts_only_expected_configuration():
  assert eligible(cp())


def test_original_camera_source_priming_and_no_echo():
  _, parser, source = primed_source()
  assert source.update(parser, 2, T + STEP + 1).valid
  assert not source.update(parser, 128, T + STEP + 1).valid
  assert not source.update(None, 2, T + STEP + 1).valid


def test_corrupted_wire_crc_withdraws_accepted_cached_source():
  packer, parser, source = primed_source()
  msg = packer.make_can_msg("SCC_CONTROL", 2, {"COUNTER": 3, "MainMode_ACC": 1, "ACCMode": 1, "aReqRaw": -2.5, "aReqValue": -0.5})
  corrupt = bytearray(msg[1])
  corrupt[16] ^= 1
  parser.update([(T + 2 * STEP, [(msg[0], bytes(corrupt), msg[2])])])
  assert parser.message_states[416].last_invalid_nanos == T + 2 * STEP
  assert not source.update(parser, 2, T + 2 * STEP).valid
  receive(packer, parser, 4, T + 3 * STEP)
  assert not source.update(parser, 2, T + 3 * STEP).valid
  receive(packer, parser, 5, T + 4 * STEP)
  assert source.update(parser, 2, T + 4 * STEP).valid


@pytest.mark.parametrize("bad_counter", [2, 12, 255])
def test_counter_repetition_or_jump_clears_source(bad_counter):
  packer, parser, source = primed_source()
  receive(packer, parser, bad_counter, T + 2 * STEP)
  assert not source.update(parser, 2, T + 2 * STEP).valid


def test_counter_wrap_is_continuous():
  packer, parser = parser_fixture()
  parser.message_states[416].counter = 253
  source = CameraSccSource(hkg_can_fd_checksum)
  for k, count in enumerate((254, 255, 0)):
    receive(packer, parser, count, T + k * STEP)
    result = source.update(parser, 2, T + k * STEP)
  assert result.valid


def test_two_valid_frames_in_one_parser_batch_do_not_look_like_a_gap():
  packer, parser, source = primed_source()

  def frame(counter):
    return packer.make_can_msg("SCC_CONTROL", 2, {"COUNTER": counter, "MainMode_ACC": 1, "ACCMode": 1, "SysFailState": 0, "aReqRaw": -2.24, "aReqValue": -0.4})

  parser.update([(T + 2 * STEP, [frame(3)]), (T + 3 * STEP, [frame(4)])])
  assert source.update(parser, 2, T + 3 * STEP).valid


def test_rejected_frame_inside_batch_breaks_source_continuity():
  packer, parser, source = primed_source()
  good = packer.make_can_msg("SCC_CONTROL", 2, {"COUNTER": 3, "MainMode_ACC": 1, "ACCMode": 1})
  bad = (good[0], bytes([good[1][0] ^ 1]) + good[1][1:], good[2])
  later = packer.make_can_msg("SCC_CONTROL", 2, {"COUNTER": 4, "MainMode_ACC": 1, "ACCMode": 1})
  parser.update([(T + 2 * STEP, [bad]), (T + 3 * STEP, [later])])
  assert not source.update(parser, 2, T + 3 * STEP).valid


@pytest.mark.parametrize("changes", [{"ACCMode": 0}, {"ACCMode": 2}, {"MainMode_ACC": 0}, {"SysFailState": 1}])
def test_source_mode_and_fault_qualification(changes):
  packer, parser, source = primed_source()
  receive(packer, parser, 3, T + 2 * STEP, **changes)
  assert not source.update(parser, 2, T + 2 * STEP).valid


def test_source_stale_and_future_are_not_retained():
  _, parser, source = primed_source()
  assert not source.update(parser, 2, T + STEP - 1).valid
  assert not source.update(parser, 2, T + STEP + SOURCE_MAX_AGE_NS + 1).valid


def test_entry_does_not_wait_for_negative_value_and_preserves_stronger_comma():
  assist = StockDecelAssist()
  assert assist.update(0.5, observation(value=0.2), T, True).accel == -2.24
  assert assist.update(-3.0, observation(T + STEP), T + STEP, True).accel == -3.0


@pytest.mark.parametrize("raw, active", [(-1.99, False), (-2.0, True), (-10.0, True)])
def test_threshold_and_acceleration_limit(raw, active):
  result = StockDecelAssist().update(0.5, observation(raw=raw), T, True)
  assert result.active == active
  assert result.accel >= -4.0


def test_raw_rebound_releases_gradually_instead_of_returning_to_acceleration():
  assist = StockDecelAssist()
  assist.update(0.5, observation(), T, True)
  result = assist.update(0.5, observation(T + STEP, 0.24, -0.42), T + STEP, True)
  assert result.active and result.accel == pytest.approx(-2.22)


def test_confirmed_fresh_clear_eventually_hands_over_without_peak_hold():
  assist = StockDecelAssist()
  assist.update(0.5, observation(), T, True)
  previous = -2.24
  phases = set()
  for k in range(1, 201):
    now = T + k * STEP
    result = assist.update(0.5, observation(now, 0.24, 0.1), now, True)
    phases.add(assist.phase)
    assert result.accel <= previous + 0.02 + 1e-9
    if k <= 10:
      assert result.accel <= 0 and assist.phase == "stock"
    previous = result.accel
  assert "handover" in phases and assist.phase == "normal" and result.accel == 0.5


@pytest.mark.parametrize(
  "source, admitted",
  [
    (Observation(T, -2.0, -0.4, False), True),
    (Observation(T + 1, -2.0, -0.4, True), True),
    (Observation(T - SOURCE_MAX_AGE_NS - 1, -2.0, -0.4, True), True),
    (Observation(T, float("nan"), -0.4, True), True),
    (Observation(T, -2.0, float("inf"), True), True),
    (Observation(T, -2.0, -0.4, True), False),
  ],
)
def test_invalid_source_or_driver_releases_latch(source, admitted):
  assist = StockDecelAssist()
  assist.update(0.5, observation(T - STEP), T - STEP, True)
  assert not assist.update(0.5, source, T, admitted).active
  assert assist.last_accel is None


def test_frozen_source_cannot_satisfy_clear_confirmation():
  assist = StockDecelAssist()
  assist.update(0.5, observation(), T, True)
  clear = observation(T + STEP, 0.24, 0.1)
  for k in range(1, 5):
    assist.update(0.5, clear, T + k * STEP, True)
    assert assist.phase != "handover"
  assert not assist.update(0.5, clear, T + 300_000_000, True).active


def admission_fixture():
  state = NS(last_invalid_nanos=0, counter_fail=0)
  parser = NS(bus=0, ts_nanos={"TCS": {"DriverBraking": T}}, vl={"TCS": {"DriverBraking": 0}}, message_states={0x175: state})
  out = NS(
    vEgo=7.0,
    vEgoRaw=7.0,
    aEgo=0.0,
    wheelSpeeds=NS(fl=7.0, fr=7.0, rl=7.0, rr=7.0),
    canValid=True,
    canTimeout=False,
    gearShifter="drive",
    gasPressed=False,
    brakePressed=False,
    parkingBrake=False,
    brakeHoldActive=False,
    cruiseState=NS(available=True),
  )
  cs = NS(out=out, cp=parser, softHoldActive=0, paddle_button_prev=0)
  cc = NS(enabled=True, longActive=True, cruiseControl=NS(override=False))
  return cc, cs


def test_driver_admission_is_independent_of_global_lateral_enabled():
  cc, cs = admission_fixture()
  assert packet_allowed(cc, cs, T, 0)
  cc.longActive = False
  assert not packet_allowed(cc, cs, T, 0)


@pytest.mark.parametrize(
  "key,value",
  [
    ("brakePressed", True),
    ("gasPressed", True),
    ("parkingBrake", True),
    ("brakeHoldActive", True),
    ("canValid", False),
    ("canTimeout", True),
    ("gearShifter", "park"),
    ("vEgo", float("nan")),
  ],
)
def test_driver_state_interlocks(key, value):
  cc, cs = admission_fixture()
  setattr(cs.out, key, value)
  assert not packet_allowed(cc, cs, T, 0)


def test_raw_driver_brake_and_tcs_rejection_are_interlocks():
  cc, cs = admission_fixture()
  cs.cp.vl["TCS"]["DriverBraking"] = 1
  assert not packet_allowed(cc, cs, T, 0)
  cs.cp.vl["TCS"]["DriverBraking"] = 0
  cs.cp.message_states[0x175].last_invalid_nanos = T + 1
  assert not packet_allowed(cc, cs, T + 1, 0)


def test_stationary_soft_hold_retains_legacy_path():
  cc, cs = admission_fixture()
  cc.enabled = cc.longActive = False
  cs.softHoldActive = 2
  cs.out.brakePressed = True
  cs.out.vEgo = cs.out.vEgoRaw = 0.0
  cs.out.wheelSpeeds = NS(fl=0.0, fr=0.0, rl=0.0, rr=0.0)
  assert packet_allowed(cc, cs, T, 0)


@pytest.mark.parametrize(
  "stamp,source_stamp,accel", [(T, T, -2.0), (T - 200_000_000, T, -2.0), (T, T - 200_000_000, -2.0), (T + 1, T, -2.0), (T, T + 1, -2.0), (T, T, float("nan"))]
)
def test_feedback_and_warning_freshness(stamp, source_stamp, accel):
  _, cs = admission_fixture()
  output = NS(pv5SccDecelEnabled=True, pv5SccDecelActive=True, pv5SccDecelSourceMonoTime=source_stamp, accel=accel)
  valid = stamp == source_stamp == T and math_finite(accel)
  assert feedback_active(cp(), output, stamp, T) == valid
  assert warning_active(cp(), cs.out, output, stamp, T) == valid


def math_finite(value):
  import math

  return math.isfinite(value)


def test_disabled_and_other_vehicle_feedback_never_activate():
  output = NS(pv5SccDecelEnabled=False, pv5SccDecelActive=True, pv5SccDecelSourceMonoTime=T, accel=-2.0)
  assert not feedback_active(cp(), output, T, T)
  output.pv5SccDecelEnabled = True
  other = NS(**(vars(cp()) | {"carFingerprint": CAR.KIA_EV6}))
  assert not feedback_active(other, output, T, T)


def packet_fixture():
  cc, cs = admission_fixture()
  cs.CP = cp()
  cs.scc_control = {"COUNTER": 10, "SysFailState": 0, "InfoDisplay": 0}
  cs.radarState = None
  cs.modelV2 = None
  cs.canfdSccHoldActive = False
  hud = NS(leadDistanceBars=2, signalStopObject=False)
  jerk = NS(carrot_cruise=0, carrot_cruise_accel=0.0, jerk_u=1.0, jerk_l=5.0)
  return cs, hud, jerk


def decoded_packet(packer, msg):
  from opendbc.can.parser import get_raw_value

  values = {}
  for name, signal in packer.dbc.addr_to_msg[416].sigs.items():
    raw = get_raw_value(msg[1], signal)
    if signal.is_signed and raw & (1 << (signal.size - 1)):
      raw -= 1 << signal.size
    values[name] = raw * signal.factor + signal.offset
  return values


def test_final_packet_disables_mode_even_when_global_enabled_or_moving_soft_hold():
  packer = CANPacker("hyundai_canfd_generated")
  cs, hud, jerk = packet_fixture()
  cs.softHoldActive = 2
  msg, value = create_acc_control_scc2(packer, NS(ECAN=0), True, -2.0, -2.0, False, False, 40.0, hud, jerk, cs, CanfdStopping(), longitudinal_allowed=False)
  d = decoded_packet(packer, msg)
  assert d["ACCMode"] == d["aReqRaw"] == d["aReqValue"] == d["StopReq"] == value == 0


@pytest.mark.parametrize("raw,previous,enabled,soft_hold", [(0.5, 0.4, True, 0), (-2.0, 0.5, True, 0), (0.0, -0.4, False, 0), (-0.5, -0.5, False, 2)])
def test_default_packet_argument_retains_legacy_bytes(raw, previous, enabled, soft_hold):
  cs, hud, jerk = packet_fixture()
  cs.softHoldActive = soft_hold
  if soft_hold:
    cs.out.vEgo = cs.out.vEgoRaw = 0.0
    cs.out.wheelSpeeds = NS(fl=0.0, fr=0.0, rl=0.0, rr=0.0)
  p1 = CANPacker("hyundai_canfd_generated")
  p2 = CANPacker("hyundai_canfd_generated")
  args = (NS(ECAN=0), enabled, previous, raw, False, False, 40.0, hud, jerk, cs)
  a = create_acc_control_scc2(p1, *args, CanfdStopping())
  b = create_acc_control_scc2(p2, *args, CanfdStopping(), longitudinal_allowed=True)
  assert a == b


def test_value_slew_is_not_confused_with_instantaneous_raw():
  packer = CANPacker("hyundai_canfd_generated")
  cs, hud, jerk = packet_fixture()
  msg, value = create_acc_control_scc2(packer, NS(ECAN=0), True, 0.5, -2.24, False, False, 40.0, hud, jerk, cs, CanfdStopping())
  d = decoded_packet(packer, msg)
  assert d["aReqRaw"] == pytest.approx(-2.24) and value == pytest.approx(0.4)


LEGACY = json.loads(Path(__file__).with_name("scc_decel_legacy_packets.json").read_text())


@pytest.mark.parametrize("case", LEGACY["cases"])
def test_disabled_bytes_match_prechange_synthetic_fixture(case):
  assert LEGACY["synthetic"] and LEGACY["source"] == "a07972357"
  speed = case["speed"]
  out = NS(
    vEgo=speed,
    vEgoRaw=speed,
    aEgo=0.0,
    wheelSpeeds=NS(fl=speed, fr=speed, rl=speed, rr=speed),
    canValid=True,
    gearShifter="drive",
    brakePressed=case["brake"],
    gasPressed=False,
    brakeHoldActive=False,
    parkingBrake=False,
    cruiseState=NS(available=True),
  )
  cs = NS(
    out=out,
    CP=None,
    softHoldActive=case["hold"],
    paddle_button_prev=0,
    canfdSccHoldActive=False,
    scc_control={"COUNTER": 17, "InfoDisplay": 0},
    radarState=None,
    modelV2=None,
  )
  hud = NS(leadDistanceBars=2, signalStopObject=False)
  jerk = NS(carrot_cruise=0, carrot_cruise_accel=0.0, jerk_u=1.0, jerk_l=5.0)
  msg, value = create_acc_control_scc2(
    CANPacker("hyundai_canfd_generated"), NS(ECAN=0), case["enabled"], 0.5, -2.24, case["stopping"], case["override"], 40.0, hud, jerk, cs, CanfdStopping()
  )
  assert msg[1].hex() == case["hex"] and value == pytest.approx(case["last_value"])


def controller_fixture(monkeypatch, enabled=True, fingerprint=CAR.KIA_PV5):
  from opendbc.car import structs, Bus
  from opendbc.car.hyundai import carcontroller

  reads = []

  def get_bool(key):
    reads.append(key)
    return bool(enabled and key == "Pv5SccDecelAssist")

  params = NS(get_bool=get_bool, get_int=lambda key: 0, get_float=lambda key: 0.0)
  monkeypatch.setattr(carcontroller, "Params", lambda: params)
  cp_value = structs.CarParams(
    brand="hyundai",
    carFingerprint=fingerprint,
    notCar=False,
    flags=int(HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC),
    openpilotLongitudinalControl=True,
    safetyConfigs=[{"safetyModel": "hyundaiCanfd", "safetyParam": 45}],
  )
  return carcontroller.CarController({Bus.pt: "hyundai_canfd_generated"}, cp_value), reads


def test_controller_disabled_path_does_not_touch_sources(monkeypatch):
  controller, _ = controller_fixture(monkeypatch, enabled=False)
  assert controller._apply_scc_decel(None, None, T, 0.53) == (0.53, True)
  assert controller.scc_decel is controller.scc_decel_source is None


def test_other_vehicle_does_not_read_new_setting(monkeypatch):
  controller, reads = controller_fixture(monkeypatch, fingerprint=CAR.KIA_EV6)
  assert "Pv5SccDecelAssist" not in reads
  assert controller._apply_scc_decel(None, None, T, 0.53) == (0.53, True)


def test_real_controller_source_to_final_CAN_and_pedal_gate(monkeypatch):
  from opendbc.car import structs

  controller, _ = controller_fixture(monkeypatch)
  cc, cs = admission_fixture()
  cc.actuators = NS(longControlState=structs.CarControl.Actuators.LongControlState.pid)
  cs.out.carrotCruise = 0
  cs.scc_control = {"COUNTER": 1, "InfoDisplay": 0}
  cs.cp = CANParser("hyundai_canfd_generated", [("TCS", 50)], 0)
  packer, cs.cp_cam = parser_fixture()
  for k in (1, 2):
    now = T + (k - 1) * STEP
    brake = packer.make_can_msg("TCS", 0, {"COUNTER": k, "DriverBraking": 0})
    cs.cp.update([(now, [brake])])
    receive(packer, cs.cp_cam, k, now)
    selected, allowed = controller._apply_scc_decel(cc, cs, now, 0.5)
  assert allowed and selected == pytest.approx(-2.24) and controller.scc_decel_decision.active
  now = T + 2 * STEP
  cs.cp.update([(now, [packer.make_can_msg("TCS", 0, {"COUNTER": 3, "DriverBraking": 1})])])
  selected, allowed = controller._apply_scc_decel(cc, cs, now, 0.5)
  assert selected == 0 and not allowed and not controller.scc_decel_decision.active
  body, hud, jerk = packet_fixture()
  msg, value = create_acc_control_scc2(
    packer, NS(ECAN=0), True, -0.4, selected, False, False, 40.0, hud, jerk, body, CanfdStopping(), longitudinal_allowed=allowed
  )
  d = decoded_packet(packer, msg)
  assert d["ACCMode"] == d["aReqRaw"] == d["aReqValue"] == value == 0
