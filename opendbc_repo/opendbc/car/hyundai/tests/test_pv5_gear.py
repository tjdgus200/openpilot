import time
from unittest.mock import Mock

import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.hyundai import carstate, hyundaicanfd
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.values import CAR, HyundaiExtFlags, HyundaiFlags
from opendbc.car.interfaces import CarInterfaceBase

Gear = structs.CarState.GearShifter


@pytest.fixture
def make_state(monkeypatch):
  params = Mock()
  params.get_int.return_value = 0
  params.get_bool.side_effect = lambda key: key == "ControlsReady"
  params.get.return_value = '{0: {}, 1: {}, 2: {}}'
  monkeypatch.setattr(carstate, "Params", lambda: params)
  monkeypatch.setattr(hyundaicanfd, "Params", lambda: params)

  def make(candidate=CAR.KIA_PV5, *, gear69=False):
    cp = structs.CarParams(carFingerprint=candidate, brand="hyundai",
                          flags=int(HyundaiFlags.CANFD | HyundaiFlags.EV | HyundaiFlags.CANFD_CAMERA_SCC |
                                    HyundaiFlags.CANFD_ALT_BUTTONS),
                          extFlags=int(HyundaiExtFlags.CANFD_GEARS_69) if gear69 else 0,
                          safetyConfigs=[{}])
    state = carstate.CarState(cp)
    parsers = state.get_can_parsers(cp)
    ci = CarInterface.__new__(CarInterface)
    ci.CP, ci.CS, ci.can_parsers = cp, state, parsers
    ci.v_ego_cluster_seen = False
    return ci, CANPacker("hyundai_canfd_generated"), time.monotonic_ns()
  return make


def frame(ci, packer, gear=5, counter=1, *, bus=None, **values):
  return packer.make_can_msg(ci.CS.gear_msg_canfd, ci.can_parsers[Bus.pt].bus if bus is None else bus,
                             {"GEAR": gear, "COUNTER": counter, **values})


def update(ci, now, *frames):
  return ci.update([(now, list(frames))])


@pytest.mark.parametrize("gear69", [False, True])
@pytest.mark.parametrize("raw,expected", [(0, Gear.park), (5, Gear.drive), (6, Gear.neutral), (7, Gear.reverse)])
def test_first_real_frame_sets_exact_gear_without_readiness_delay(make_state, gear69, raw, expected):
  ci, packer, now = make_state(gear69=gear69)
  assert not ci.CS.pv5_gear_valid
  ret = update(ci, now, frame(ci, packer, raw))
  assert ci.CS.controls_ready_count == 1
  assert ret.gearShifter == expected
  assert ci.CS.pv5_gear_valid
  assert ret.canValid
  parser = ci.can_parsers[Bus.pt]
  state = parser.message_states[parser.dbc.name_to_msg[ci.CS.gear_msg_canfd].address]
  assert not state.ignore_alive and not state.ignore_checksum
  assert state.ignore_counter == (not gear69)
  assert state.timeout_threshold == carstate.PV5_GEAR_MAX_AGE_NS == 200_000_000


@pytest.mark.parametrize("gear69", [False, True])
def test_missing_and_late_first_receive_after_discovery_window(make_state, gear69):
  ci, packer, now = make_state(gear69=gear69)
  ret = update(ci, now)
  assert ret.gearShifter == Gear.unknown
  assert not ret.canValid and not ci.CS.pv5_gear_valid
  ci.CS.controls_ready_count = carstate.READY_COUNT_OK + 1
  ret = update(ci, now + 20_000_000_000)
  assert ret.gearShifter == Gear.unknown and not ret.canValid
  ret = update(ci, now + 20_020_000_000, frame(ci, packer))
  assert ret.gearShifter == Gear.drive and ci.CS.pv5_gear_valid


def test_pedal_and_gear_use_live_mapping_before_count125(make_state):
  ci, packer, now = make_state()
  ret = update(ci, now, frame(ci, packer, ACCELERATOR_PEDAL=128))
  assert ret.gasPressed and ret.gas == pytest.approx(128 / 255)
  assert ret.gearShifter == Gear.drive
  ret = update(ci, now + 20_000_000, frame(ci, packer, gear=6, counter=2, ACCELERATOR_PEDAL=0))
  assert not ret.gasPressed and ret.gearShifter == Gear.neutral
  assert ci.CS.accelerator is ci.can_parsers[Bus.pt].vl["ACCELERATOR"]


@pytest.mark.parametrize("age,expected", [(0, True), (199_999_999, True), (200_000_000, True), (200_000_001, False)])
def test_freshness_boundary_even_when_other_bus_frames_keep_arriving(make_state, age, expected):
  ci, packer, now = make_state()
  update(ci, now, frame(ci, packer))
  # Non-gear traffic cannot refresh a cached gear.
  ret = update(ci, now + age, (0x7FF, b"", ci.can_parsers[Bus.pt].bus))
  assert ci.CS.pv5_gear_valid == expected
  assert (ret.gearShifter == Gear.drive) == expected
  if not expected:
    assert not ret.canValid


def test_existing_stricter_parser_timeout_is_not_widened(make_state):
  ci, packer, now = make_state()
  msg = frame(ci, packer)
  update(ci, now, msg)
  ci.can_parsers[Bus.pt].message_states[msg[0]].timeout_threshold = 50_000_000
  ret = update(ci, now + 50_000_001, (0x7FF, b"", ci.can_parsers[Bus.pt].bus))
  assert ret.gearShifter == Gear.unknown and not ret.canValid


def test_bad_crc_does_not_refresh_and_valid_next_frame_recovers(make_state):
  ci, packer, now = make_state()
  valid = frame(ci, packer)
  update(ci, now, valid)
  bad = frame(ci, packer, gear=0, counter=2)
  bad = (bad[0], bytes([bad[1][0] ^ 1]) + bad[1][1:], bad[2])
  parser = ci.can_parsers[Bus.pt]
  old_stamp = parser.ts_nanos["ACCELERATOR"]["GEAR"]
  ret = update(ci, now + 20_000_000, bad)
  assert parser.ts_nanos["ACCELERATOR"]["GEAR"] == old_stamp
  assert parser.dat[valid[0]] == valid[1]
  assert ret.gearShifter == Gear.unknown and not ret.canValid
  ret = update(ci, now + 40_000_000, frame(ci, packer, counter=3))
  assert ret.gearShifter == Gear.drive and ci.CS.pv5_gear_valid


def test_counter_rejection_does_not_refresh_and_then_recovers(make_state):
  ci, packer, now = make_state(gear69=True)
  repeated = frame(ci, packer)
  update(ci, now, repeated)
  for i in range(1, 6):
    ret = update(ci, now + i * 20_000_000, repeated)
  parser = ci.can_parsers[Bus.pt]
  assert parser.ts_nanos["GEAR"]["GEAR"] < now + 100_000_000
  assert ret.gearShifter == Gear.unknown and not ret.canValid
  ret = update(ci, now + 120_000_000, frame(ci, packer, counter=2))
  assert ret.gearShifter == Gear.drive and ci.CS.pv5_gear_valid


@pytest.mark.parametrize("order", ["valid_bad", "bad_valid"])
def test_invalid_same_timestamp_cannot_reopen_gate(make_state, order):
  ci, packer, now = make_state()
  valid = frame(ci, packer)
  bad = (valid[0], bytes([valid[1][0] ^ 1]) + valid[1][1:], valid[2])
  frames = [valid, bad] if order == "valid_bad" else [bad, valid]
  ret = update(ci, now, *frames)
  assert ret.gearShifter == Gear.unknown and not ret.canValid


@pytest.mark.parametrize("bus", [1, 2, 128, 129, 130])
def test_wrong_bus_and_transmit_echo_cannot_initialize(make_state, bus):
  ci, packer, now = make_state()
  assert ci.can_parsers[Bus.pt].bus != bus
  ret = update(ci, now, frame(ci, packer, bus=bus))
  assert ret.gearShifter == Gear.unknown and not ret.canValid


@pytest.mark.parametrize("gear69", [False, True])
@pytest.mark.parametrize("raw", [1, 2, 3, 4])
def test_undefined_gear_cannot_become_drive_from_gear_step(make_state, gear69, raw):
  ci, packer, now = make_state(gear69=gear69)
  ci.CS.GEAR = True
  gear_step = packer.make_can_msg("GEAR", ci.can_parsers[Bus.pt].bus, {"GEAR": raw, "GEAR_STEP": 3, "COUNTER": 1})
  ci.can_parsers[Bus.pt].vl["GEAR"]  # Register the independent step source before receive.
  ret = update(ci, now, gear_step, frame(ci, packer, gear=raw))
  assert ret.gearShifter == Gear.unknown and not ret.canValid


def test_future_timestamp_and_wrong_dlc_remain_unknown(make_state):
  ci, packer, now = make_state()
  msg = frame(ci, packer)
  update(ci, now, msg)
  ret = update(ci, now - 1)
  assert ret.gearShifter == Gear.unknown and not ret.canValid
  parser = ci.can_parsers[Bus.pt]
  parser.dat[msg[0]] = msg[1][:-1]
  ret = update(ci, now + 10_000_000)
  assert ret.gearShifter == Gear.unknown and not ret.canValid


def test_other_ev_retains_existing_discovery_and_initial_gear(make_state):
  ci, packer, now = make_state(CAR.KIA_EV6)
  assert "ACCELERATOR" not in ci.can_parsers[Bus.pt].vl
  ret = update(ci, now, frame(ci, packer))
  assert ret.gearShifter == Gear.park
  assert ci.CS.accelerator is None
  assert ret.canValid


@pytest.mark.parametrize("candidate", [CAR.KIA_PV5, CAR.KIA_EV6])
@pytest.mark.parametrize("base_valid", [False, True])
@pytest.mark.parametrize("gear_valid", [False, True])
def test_interface_never_relaxes_existing_can_validity(make_state, monkeypatch, candidate, base_valid, gear_valid):
  ci, _, _ = make_state(candidate)
  ret = structs.CarState(canValid=base_valid)
  monkeypatch.setattr(CarInterfaceBase, "update", lambda self, packets: ret)
  ci.CS.pv5_gear_valid = gear_valid
  assert ci.update([]).canValid == (base_valid and (candidate != CAR.KIA_PV5 or gear_valid))


def test_accelerator_preserves_existing_forwarded_counter_policy(make_state):
  ci, packer, now = make_state()
  for i in range(300):
    ret = update(ci, now + i * 20_000_000, frame(ci, packer, counter=(2*i) % 256))
    assert ret.gearShifter == Gear.drive and ci.CS.pv5_gear_valid
  parser = ci.can_parsers[Bus.pt]
  assert parser.message_states[53].ignore_counter
  # Skipping counter validation never skips CRC or the accepted-frame clock.
  valid_stamp = parser.ts_nanos["ACCELERATOR"]["GEAR"]
  bad = frame(ci, packer, counter=88)
  bad = (bad[0], bytes([bad[1][0] ^ 1]) + bad[1][1:], bad[2])
  ret = update(ci, now + 6_000_000_000, bad)
  assert parser.ts_nanos["ACCELERATOR"]["GEAR"] == valid_stamp
  assert not ret.canValid and ret.gearShifter == Gear.unknown


@pytest.mark.parametrize("length", [8, 12, 16, 20, 24, 48, 64])
def test_actual_wrong_dlc_with_matching_crc_is_not_a_valid_gear(make_state, length):
  ci, packer, now = make_state()
  valid = frame(ci, packer)
  update(ci, now, valid)
  data = bytearray(frame(ci, packer, counter=2)[1])
  data = data[:length] if length < len(data) else data + bytes(length-len(data))
  data[:2] = hyundaicanfd.hkg_can_fd_checksum(valid[0], None, data).to_bytes(2, "little")
  ret = update(ci, now + 20_000_000, (valid[0], bytes(data), valid[2]))
  # The general parser accepts this CRC, but exact message size is required.
  assert ci.can_parsers[Bus.pt].ts_nanos["ACCELERATOR"]["GEAR"] == now + 20_000_000
  assert ret.gearShifter == Gear.unknown and not ret.canValid
  ret = update(ci, now + 40_000_000, frame(ci, packer, counter=3))
  assert ret.gearShifter == Gear.drive and ci.CS.pv5_gear_valid


def test_existing_can_failure_remains_no_entry_and_immediate_disable():
  from openpilot.selfdrive.selfdrived.events import EVENTS, ET, EventName
  assert ET.NO_ENTRY in EVENTS[EventName.canError]
  assert ET.IMMEDIATE_DISABLE in EVENTS[EventName.canError]


@pytest.mark.parametrize("raw,event", [(0, "wrongGear"), (6, "wrongGear"), (7, "reverseGear")])
def test_actual_park_neutral_reverse_keep_common_gear_events(make_state, raw, event):
  from openpilot.selfdrive.car.car_specific import CarSpecificEvents, EventName
  ci, packer, now = make_state()
  ret = update(ci, now, frame(ci, packer, gear=raw))
  events = CarSpecificEvents(ci.CP).create_common_events(ret, structs.CarState())
  assert getattr(EventName, event) in events.names


def test_existing_rate_learning_and_tighter_timeout_are_preserved(make_state):
  ci, packer, now = make_state()
  parser = ci.can_parsers[Bus.pt]
  from opendbc.can import CANParser
  original = CANParser(parser.dbc_name, [], parser.bus)
  original._add_message("ACCELERATOR", ignore_counter=True)
  for i in range(70):
    msg = frame(ci, packer, counter=(2*i) % 256)
    stamp = now + i * 20_000_000
    original.update([(stamp, [msg])])
    update(ci, stamp, msg)
  before = original.message_states[53]
  after = parser.message_states[53]
  assert before.frequency == after.frequency > 50
  assert before.timeout_threshold == after.timeout_threshold < 200_000_000
  stamp = now + 69 * 20_000_000 + int(before.timeout_threshold) + 1
  ret = update(ci, stamp, (0x7FF, b"", parser.bus))
  assert ret.gearShifter == Gear.unknown and not ret.canValid


@pytest.mark.parametrize("empty_kind", ["no_packets", "empty_batch", "same_packet"])
def test_frozen_can_clock_cannot_keep_last_gear_fresh(make_state, monkeypatch, empty_kind):
  ci, packer, stamp = make_state()
  clock = [10_000_000_000]
  monkeypatch.setattr(carstate.time, "monotonic_ns", lambda: clock[0])
  msg = frame(ci, packer)
  update(ci, stamp, msg)
  assert ci.CS.pv5_gear_valid
  clock[0] += 200_000_001
  packets = [] if empty_kind == "no_packets" else [(stamp, [] if empty_kind == "empty_batch" else [msg])]
  ret = ci.update(packets)
  assert not ret.canValid and ret.gearShifter == Gear.unknown
  assert ci.can_parsers[Bus.pt].ts_nanos["ACCELERATOR"]["GEAR"] == stamp
  ret = update(ci, stamp + 220_000_000, frame(ci, packer, counter=3))
  assert ret.gearShifter == Gear.drive and ci.CS.pv5_gear_valid


def test_already_aged_first_packet_does_not_get_a_new_full_lifetime(make_state, monkeypatch):
  ci, packer, stamp = make_state()
  clock = [10_000_000_000]
  monkeypatch.setattr(carstate.time, "monotonic_ns", lambda: clock[0])
  ret = ci.update([(stamp, [frame(ci, packer)]), (stamp + 150_000_000, [])])
  assert ret.gearShifter == Gear.drive
  clock[0] += 50_000_001
  ret = ci.update([])
  assert ret.gearShifter == Gear.unknown and not ret.canValid


def test_local_clock_rollback_and_old_source_stamp_do_not_refresh(make_state, monkeypatch):
  ci, packer, stamp = make_state()
  clock = [10_000_000_000]
  monkeypatch.setattr(carstate.time, "monotonic_ns", lambda: clock[0])
  update(ci, stamp, frame(ci, packer))
  clock[0] -= 1
  assert ci.update([]).gearShifter == Gear.unknown
  clock[0] += 2
  ret = update(ci, stamp - 1, frame(ci, packer, counter=3))
  assert ret.gearShifter == Gear.unknown and not ret.canValid
