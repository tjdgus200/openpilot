"""PC controller/CAN/production safety-hook regressions, no vehicle connection."""
import ctypes
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS

import pytest

from opendbc.can import CANPacker
from opendbc.car import structs
from opendbc.car.hyundai import scc_decel
from opendbc.car.hyundai.hyundaicanfd import create_acc_control_scc2
from opendbc.car.hyundai.stopping import CanfdStopping
from opendbc.car.hyundai.tests.test_scc_decel import (
  T, STEP, admission_fixture, packet_fixture, decoded_packet, controller_fixture, receive, parser_fixture,
)


def override_fixture():
  cc, cs = admission_fixture()
  cc.longActive = False
  cc.cruiseControl.override = True
  cs.out.gasPressed = True
  cs.out.carrotCruise = 0
  return cc, cs


def send_override(cc, cs, *, previous=-2.0, stopping=True, qualified=None):
  body, hud, jerk = packet_fixture()
  for k, v in vars(body).items():
    if not hasattr(cs, k):
      setattr(cs, k, v)
  admitted = scc_decel.packet_allowed(cc, cs, T, 0)
  qualified = scc_decel.gas_override_allowed(cc, cs, T, 0) if qualified is None else qualified
  packer = CANPacker("hyundai_canfd_generated")
  msg, value = create_acc_control_scc2(
    packer, NS(ECAN=0), cc.enabled, previous, -3.5, stopping, cc.cruiseControl.override,
    40.0, hud, jerk, cs, CanfdStopping(), longitudinal_allowed=admitted, gas_override_allowed=qualified,
  )
  return msg, decoded_packet(packer, msg), value


def test_gas_held_enable_preserves_mode_2_without_actuation():
  cc, cs = override_fixture()
  assert not scc_decel.packet_allowed(cc, cs, T, 0)
  assert scc_decel.gas_override_allowed(cc, cs, T, 0)
  _, d, value = send_override(cc, cs)
  assert d["ACCMode"] == 2
  assert value == 0
  for name in ("aReqRaw", "aReqValue", "StopReq", "AccelLimitBandLower", "AccelLimitBandUpper"):
    assert d[name] == 0


@pytest.mark.parametrize("target,key,value", [
  ("cc", "enabled", False), ("cc", "longActive", True),
  ("cruise", "override", False), ("cruise", "cancel", True), ("out", "gasPressed", False),
  ("out", "brakePressed", True), ("out", "parkingBrake", True),
  ("out", "brakeHoldActive", True), ("out", "canValid", False),
  ("out", "canTimeout", True), ("out", "gearShifter", "park"),
  ("out", "gearShifter", "reverse"), ("out", "gearShifter", "neutral"),
  ("out", "carrotCruise", 1), ("out", "carrotCruise", 2),
  ("out", "aEgo", float("nan")), ("out", "vEgo", float("nan")), ("out", "vEgoRaw", float("inf")),
  ("wheel", "fl", float("nan")), ("cs", "paddle_button_prev", 1),
  ("cs", "softHoldActive", 1),
])
def test_denied_driver_states_do_not_rearm(target, key, value):
  cc, cs = override_fixture()
  targets = dict(cc=cc, cruise=cc.cruiseControl, out=cs.out, wheel=cs.out.wheelSpeeds, cs=cs)
  setattr(targets[target], key, value)
  assert not scc_decel.gas_override_allowed(cc, cs, T, 0)
  _, d, value = send_override(cc, cs)
  assert d["ACCMode"] == d["aReqRaw"] == d["aReqValue"] == d["StopReq"] == value == 0


@pytest.mark.parametrize("fault", ["stale", "future", "invalid", "counter", "brake", "bus", "missing"])
def test_override_requires_fresh_accepted_driver_brake(fault):
  cc, cs = override_fixture()
  if fault == "stale": cs.cp.ts_nanos["TCS"]["DriverBraking"] = T - 100_000_001
  if fault == "future": cs.cp.ts_nanos["TCS"]["DriverBraking"] = T + 1
  if fault == "invalid": cs.cp.message_states[0x175].last_invalid_nanos = T
  if fault == "counter": cs.cp.message_states[0x175].counter_fail = 1
  if fault == "brake": cs.cp.vl["TCS"]["DriverBraking"] = 1
  if fault == "bus": cs.cp.bus = 2
  if fault == "missing": cs.cp = None
  assert not scc_decel.gas_override_allowed(cc, cs, T, 0)
  _, d, _ = send_override(cc, cs)
  assert d["ACCMode"] == d["aReqRaw"] == d["aReqValue"] == d["StopReq"] == 0


@pytest.mark.parametrize("fault", ["disabled", "gas", "override", "unavailable", "paddle", "parking", "hold", "carrot", "soft"])
def test_final_packet_does_not_accept_inconsistent_override_admission(fault):
  cc, cs = override_fixture()
  body, hud, jerk = packet_fixture()
  for k, v in vars(body).items():
    if not hasattr(cs, k): setattr(cs, k, v)
  if fault == "disabled": cc.enabled = False
  if fault == "gas": cs.out.gasPressed = False
  if fault == "override": cc.cruiseControl.override = False
  if fault == "unavailable": cs.out.cruiseState.available = False
  if fault == "paddle": cs.paddle_button_prev = 1
  if fault == "parking": cs.out.parkingBrake = True
  if fault == "hold": cs.out.brakeHoldActive = True
  if fault == "carrot": jerk.carrot_cruise = 1
  if fault == "soft": cs.softHoldActive = 1
  p = CANPacker("hyundai_canfd_generated")
  msg, _ = create_acc_control_scc2(p, NS(ECAN=0), cc.enabled, -2.0, -4.0, True, cc.cruiseControl.override,
                                 40.0, hud, jerk, cs, CanfdStopping(), longitudinal_allowed=False, gas_override_allowed=True)
  d = decoded_packet(p, msg)
  assert d["ACCMode"] == d["StopReq"] == d["aReqRaw"] == d["aReqValue"] == 0


def test_real_controller_discards_stock_braking_until_pedal_release(monkeypatch):
  controller, _ = controller_fixture(monkeypatch)
  cc, cs = override_fixture()
  cc.actuators = NS(longControlState=structs.CarControl.Actuators.LongControlState.pid)
  cs.scc_control = {"COUNTER": 1, "InfoDisplay": 0}
  packer, cs.cp_cam = parser_fixture()
  for k in (1, 2):
    now = T + (k - 1) * STEP
    cs.cp.ts_nanos["TCS"]["DriverBraking"] = now
    receive(packer, cs.cp_cam, k, now, aReqRaw=-3.5)
    selected, admitted = controller._apply_scc_decel(cc, cs, now, 0.5)
    assert not admitted and selected == 0 and not controller.scc_decel_decision.active
    assert controller.scc_decel.last_accel is None
  cc.longActive = True
  cc.cruiseControl.override = cs.out.gasPressed = False
  now = T + 2 * STEP
  cs.cp.ts_nanos["TCS"]["DriverBraking"] = now
  receive(packer, cs.cp_cam, 3, now, aReqRaw=-3.5)
  selected, admitted = controller._apply_scc_decel(cc, cs, now, 0.5)
  assert admitted and selected == pytest.approx(-3.5) and controller.scc_decel_decision.active


@pytest.fixture(scope="session")
def panda_native(tmp_path_factory):
  root = next(p for p in Path(__file__).resolve().parents if (p / "panda/board").is_dir())
  folder = tmp_path_factory.mktemp("scc-native")
  source = folder / "scc.c"
  source.write_text('''#include "hyundai_canfd_alt_buttons.c"
EXPORT int pc_rx(int address, int bus, int length, const uint8_t *data, uint32_t now) {
  timer.CNT = now;
  CANPacket_t pkt = packet(address, bus, length, data);
  return safety_rx_hook(&pkt);
}
EXPORT bool pc_controls_allowed(void) { return controls_allowed; }
EXPORT void pc_clear_controls(void) { controls_allowed = false; }
EXPORT void pc_alternative(int value) { alternative_experience = value; }
''')
  output = folder / "scc.so"
  subprocess.run(["cc", "-shared", "-fPIC", "-O2", f"-I{root / 'panda/board'}", f"-I{root / 'panda'}",
                  f"-I{root / 'opendbc_repo/opendbc/safety'}", f"-I{root / 'opendbc_repo/opendbc/safety/tests'}",
                  str(source), "-o", str(output)], check=True)
  lib = ctypes.CDLL(str(output))
  lib.alt2_test_init.argtypes = [ctypes.c_int]
  lib.alt2_test_tx.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32]
  lib.pc_rx.argtypes = lib.alt2_test_tx.argtypes
  lib.pc_controls_allowed.restype = ctypes.c_bool
  lib.pc_alternative.argtypes = [ctypes.c_int]
  return lib


@pytest.mark.parametrize("alternative", [0, 1])
def test_actual_panda_hook_accepts_zero_override_mode_and_cancel_does_not_rearm(panda_native, alternative):
  lib = panda_native
  lib.alt2_test_init(45)
  lib.pc_alternative(alternative)
  lib.pc_clear_controls()
  cc, cs = override_fixture()
  msg, d, _ = send_override(cc, cs)
  data = (ctypes.c_uint8 * len(msg[1])).from_buffer_copy(msg[1])
  assert lib.alt2_test_tx(msg[0], msg[2], len(msg[1]), data, 1000) & 1
  assert d["ACCMode"] == 2 and lib.pc_controls_allowed()
  cc.enabled = False
  lib.pc_clear_controls()
  msg, d, _ = send_override(cc, cs)
  data = (ctypes.c_uint8 * len(msg[1])).from_buffer_copy(msg[1])
  assert lib.alt2_test_tx(msg[0], msg[2], len(msg[1]), data, 21000) & 1
  assert d["ACCMode"] == 0 and not lib.pc_controls_allowed()


@pytest.mark.parametrize("alternative,expected", [(0, False), (1, True)])
def test_actual_panda_gas_rx_retains_user_accelerator_disengagement_policy(panda_native, alternative, expected):
  lib = panda_native
  lib.alt2_test_init(45)
  lib.pc_alternative(alternative)
  cc, cs = override_fixture()
  msg, _, _ = send_override(cc, cs)
  data = (ctypes.c_uint8 * len(msg[1])).from_buffer_copy(msg[1])
  assert lib.alt2_test_tx(msg[0], msg[2], len(msg[1]), data, 1000) & 1
  packer = CANPacker("hyundai_canfd_generated")
  msg = packer.make_can_msg("ACCELERATOR", 0, {"ACCELERATOR_PEDAL": 5})
  data = (ctypes.c_uint8 * len(msg[1])).from_buffer_copy(msg[1])
  assert lib.pc_rx(msg[0], msg[2], len(msg[1]), data, 1100)
  assert lib.pc_controls_allowed() == expected


@pytest.mark.parametrize("fault", ["aEgo", "previous", "jerk"])
def test_final_override_does_not_revive_a_stop_controller_numeric_fault(fault):
  cc, cs = override_fixture()
  body, hud, jerk = packet_fixture()
  for k, v in vars(body).items():
    if not hasattr(cs, k): setattr(cs, k, v)
  previous = -2.0
  if fault == "aEgo": cs.out.aEgo = float("nan")
  if fault == "previous": previous = float("nan")
  if fault == "jerk": jerk.jerk_u = float("nan")
  p = CANPacker("hyundai_canfd_generated")
  msg, _ = create_acc_control_scc2(p, NS(ECAN=0), True, previous, 0.0, False, True,
                                 40.0, hud, jerk, cs, CanfdStopping(), longitudinal_allowed=False, gas_override_allowed=True)
  d = decoded_packet(p, msg)
  assert d["ACCMode"] == d["StopReq"] == d["aReqRaw"] == d["aReqValue"] == 0
