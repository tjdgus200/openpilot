import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car import structs
from opendbc.car.hyundai.hyundaicanfd import hkg_can_fd_checksum
from opendbc.car.hyundai.rear_gap import REAR_INPUT_MAX_AGE_NS, pv5_oem_bsd_inputs


NAME = "BLINDSPOTS_REAR_CORNERS"
ADDRESS = 0x1BA
BUS = 2
START = 10_000_000_000
STEP = 20_000_000
INVALID = (0, False, False)


@pytest.fixture
def wire():
  return CANPacker("hyundai_canfd_generated"), CANParser("hyundai_canfd_generated", [(NAME, 50)], BUS)


def receive(wire, stamp, counter=1, bus=BUS, **fields):
  packer, parser = wire
  parser.update([(stamp, [packer.make_can_msg(NAME, bus, dict(fields, COUNTER=counter))])])
  return parser


@pytest.mark.parametrize("field,left,right", [
  ("FL_INDICATOR", True, False), ("INDICATOR_LEFT_TWO", True, False), ("INDICATOR_LEFT_FOUR", True, False),
  ("FR_INDICATOR", False, True), ("INDICATOR_RIGHT_TWO", False, True), ("INDICATOR_RIGHT_FOUR", False, True),
])
def test_each_oem_warning_has_its_own_side(wire, field, left, right):
  parser = receive(wire, START, **{field: 1})
  assert pv5_oem_bsd_inputs(parser, START) == (START, left, right)


def test_active_both_and_clear_transition(wire):
  parser = receive(wire, START, INDICATOR_LEFT_TWO=1, INDICATOR_RIGHT_TWO=1)
  assert pv5_oem_bsd_inputs(parser, START) == (START, True, True)
  receive(wire, START + STEP, counter=2)
  assert pv5_oem_bsd_inputs(parser, START + STEP) == (START + STEP, False, False)


def test_freshness_and_bus(wire):
  parser = receive(wire, START, bus=BUS - 1, FL_INDICATOR=1)
  assert pv5_oem_bsd_inputs(parser, START) == INVALID
  receive(wire, START, FL_INDICATOR=1)
  assert pv5_oem_bsd_inputs(parser, START - 1) == INVALID
  assert pv5_oem_bsd_inputs(parser, START + REAR_INPUT_MAX_AGE_NS) == (START, True, False)
  assert pv5_oem_bsd_inputs(parser, START + REAR_INPUT_MAX_AGE_NS + 1) == INVALID
  assert pv5_oem_bsd_inputs(None, START) == INVALID


def test_newer_bad_checksum_cannot_hide_behind_the_fresh_cached_warning(wire):
  packer, parser = wire
  receive(wire, START, FR_INDICATOR=1)
  address, bad, bus = packer.make_can_msg(NAME, BUS, {"COUNTER": 2, "FR_INDICATOR": 1})
  parser.update([(START + STEP, [(address, bytes([bad[0] ^ 1]) + bad[1:], bus)])])
  assert parser.ts_nanos[NAME]["COUNTER"] == START
  assert pv5_oem_bsd_inputs(parser, START + STEP) == INVALID
  receive(wire, START + 2 * STEP, counter=3, FR_INDICATOR=1)
  assert pv5_oem_bsd_inputs(parser, START + 2 * STEP) == (START + 2 * STEP, False, True)


def test_wrong_dlc_is_invalid_even_with_a_correct_checksum(wire):
  packer, parser = wire
  address, data, bus = packer.make_can_msg(NAME, BUS, {"COUNTER": 1, "FR_INDICATOR": 1})
  payload = bytearray(data + bytes(8))
  payload[:2] = hkg_can_fd_checksum(ADDRESS, None, payload).to_bytes(2, "little")
  parser.update([(START, [(address, bytes(payload), bus)])])
  assert len(parser.dat[ADDRESS]) == 32
  assert pv5_oem_bsd_inputs(parser, START) == INVALID


def test_duplicate_counter_is_invalid_even_with_a_correct_checksum(wire):
  parser = receive(wire, START, FR_INDICATOR=1)
  receive(wire, START + STEP, FR_INDICATOR=1)
  assert parser.ts_nanos[NAME]["COUNTER"] == START + STEP
  assert pv5_oem_bsd_inputs(parser, START + STEP) == INVALID


def test_missing_decoded_field_is_invalid(wire):
  parser = receive(wire, START, FR_INDICATOR=1)
  del parser.vl[NAME]["FL_INDICATOR"]
  assert pv5_oem_bsd_inputs(parser, START) == INVALID


def test_schema_roundtrip_and_legacy_defaults():
  cs = structs.CarState.new_message()
  assert (cs.pv5OemBlindspotValid, cs.pv5OemBlindspotLeft, cs.pv5OemBlindspotRight, cs.pv5OemBlindspotMonoTime) == (
    False, False, False, 0)
  cs.pv5OemBlindspotValid = True
  cs.pv5OemBlindspotRight = True
  cs.pv5OemBlindspotMonoTime = START
  with structs.CarState.from_bytes(cs.to_bytes()) as decoded:
    assert decoded.pv5OemBlindspotValid and decoded.pv5OemBlindspotRight
    assert not decoded.pv5OemBlindspotLeft and decoded.pv5OemBlindspotMonoTime == START
