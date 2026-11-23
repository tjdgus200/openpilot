import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car import structs
from opendbc.car.hyundai.pv5_lane_marking import Pv5LaneMarking

DBC = 'hyundai_canfd_generated'
PREFERRED = 'CAM_0x2a4'
FALLBACK = 'CAM_0x362'
TICK = 50_000_000


@pytest.fixture
def lane_input():
  camera, alt = (CANParser(DBC, [], bus) for bus in (2, 1))
  return Pv5LaneMarking(camera, alt), CANPacker(DBC), camera, alt


def lane_frame(packer, message, bus, *, count=1, left=0, right=1, color=1, confidence=3):
  return packer.make_can_msg(message, bus, {
    'COUNTER': count, 'LEFT_LANE_TYPE': left, 'RIGHT_LANE_TYPE': right,
    'LEFT_LANE_COLOR': color, 'RIGHT_LANE_COLOR': color,
    'LEFT_LANE_LINE': confidence, 'RIGHT_LANE_LINE': confidence,
  })


@pytest.mark.parametrize('message', [PREFERRED, FALLBACK])
@pytest.mark.parametrize('bus', [1, 2])
def test_late_first_frame_needs_no_car_state_startup_discovery(lane_input, message, bus):
  reader, packer, camera, alt = lane_input
  assert reader.update([]) == (0, -1, -1)
  assert reader.update([(10 * TICK, [])]) == (0, -1, -1)
  frame = lane_frame(packer, message, bus)
  assert reader.update([(1000 * TICK, [frame])]) == (1000 * TICK, 0, 1)
  assert not camera.addresses and not alt.addresses


def test_newest_original_frame_wins_and_same_timestamp_conflict_is_per_side(lane_input):
  reader, packer, *_ = lane_input
  old = lane_frame(packer, PREFERRED, 1)
  live = lane_frame(packer, FALLBACK, 2, left=1, right=5)
  assert reader.update([(TICK, [old]), (2 * TICK, [live])]) == (2 * TICK, 1, 5)
  newer = lane_frame(packer, PREFERRED, 1, count=2, left=0, right=5)
  assert reader.update([(2 * TICK, [newer])]) == (2 * TICK, -1, 5)


def test_out_of_order_packet_cannot_replace_current_observation(lane_input):
  reader, packer, *_ = lane_input
  newest = lane_frame(packer, PREFERRED, 1, left=1)
  delayed = lane_frame(packer, PREFERRED, 1, count=2, left=0)
  assert reader.update([(2 * TICK, [newest]), (TICK, [delayed])]) == (2 * TICK, 1, 1)


def test_new_rejected_packets_invalidate_previous_sample_and_valid_frame_recovers(lane_input):
  reader, packer, *_ = lane_input
  address, payload, bus = lane_frame(packer, PREFERRED, 1)
  bad_crc = (address, bytes([payload[0] ^ 1]) + payload[1:], bus)
  bad_dlc = (address, payload[:-4], bus)
  first = lane_frame(packer, PREFERRED, 1, count=2)
  assert reader.update([(TICK, [bad_crc]), (2 * TICK, [first]), (3 * TICK, [bad_dlc])]) == (0, -1, -1)
  recovered = lane_frame(packer, PREFERRED, 1, count=3)
  assert reader.update([(4 * TICK, [recovered])]) == (4 * TICK, 0, 1)
  assert reader.update([(5 * TICK, [bad_crc])]) == (0, -1, -1)


@pytest.mark.parametrize('color,confidence', [(0, 3), (1, 0), (15, 1)])
def test_undetected_and_reserved_marking_is_not_dashed(lane_input, color, confidence):
  reader, packer, *_ = lane_input
  frame = lane_frame(packer, PREFERRED, 1, color=color, confidence=confidence)
  assert reader.update([(TICK, [frame])]) == (TICK, -1, -1)


def test_reject_transmit_echo_and_unconfigured_bus(lane_input):
  reader, packer, *_ = lane_input
  ignored = [lane_frame(packer, PREFERRED, bus) for bus in (0, 129, 130)]
  assert reader.update([(TICK, ignored)]) == (0, -1, -1)


def test_raw_type_has_no_decimal_alias_and_old_schema_defaults_are_unknown(lane_input):
  reader, packer, *_ = lane_input
  frame = lane_frame(packer, PREFERRED, 1, left=10, right=15)
  assert reader.update([(TICK, [frame])]) == (TICK, 10, 15)
  state = structs.CarState()
  assert state.pv5LaneMarking.monoTime == 0
  assert state.pv5LaneMarking.leftType == state.pv5LaneMarking.rightType == -1
  state.pv5LaneMarking.monoTime = TICK
  state.pv5LaneMarking.leftType = 0
  state.pv5LaneMarking.rightType = 5
  with structs.CarState.from_bytes(state.to_bytes()) as decoded:
    assert (decoded.pv5LaneMarking.monoTime, decoded.pv5LaneMarking.leftType,
            decoded.pv5LaneMarking.rightType) == (TICK, 0, 5)
