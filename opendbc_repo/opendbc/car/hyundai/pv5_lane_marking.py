"""Optional original PV5 lane-marking observations on the camera and A-CAN buses."""

from collections import defaultdict
import math

from opendbc.can import CANParser
from opendbc.car.hyundai.lane_line_time import PV5_CLUSTER_DASHED, PV5_LANE_MARKING_MAX_AGE_NS

LANE_MESSAGES = ('CAM_0x2a4', 'CAM_0x362')
PV5_CAMERA_LANE_ADDRESS = 0x161
PV5_CLUSTER_SOLID_DISPLAY = 2
CLUSTER_SOLID_CONFIRM_NS = 150_000_000
CLUSTER_DISPLAY_MAX_GAP_NS = 125_000_000
UNKNOWN_DISPLAY = (0, -1, -1)


def _dedicated_display_code(signals, side):
  color = int(signals[f'{side}_LANE_COLOR'])
  kind = int(signals[f'{side}_LANE_TYPE'])
  confidence = int(signals[f'{side}_LANE_LINE'])
  if color not in (1, 2, 3) or confidence < 2 or kind not in (0, 1, 4, 5):
    return -1
  return color * 10 + (1 if kind == 4 else kind)


class Pv5LaneMarking:
  def __init__(self, camera_parser, alt_parser):
    self.camera_parser = camera_parser
    # Register both formats from startup. These independent parsers are never
    # used for the vehicle CAN validity or transmit templates.
    self.parsers = {
      parser.bus: CANParser(parser.dbc_name, [(name, math.nan) for name in LANE_MESSAGES], parser.bus)
      for parser in (camera_parser, alt_parser)
    }
    self.addresses = {
      bus: {parser.dbc.name_to_msg[name].address: parser.dbc.name_to_msg[name].size for name in LANE_MESSAGES}
      for bus, parser in self.parsers.items()
    }
    self.last_seen = {}
    self.invalid_dlc = {}
    self.display = UNKNOWN_DISPLAY
    self.camera = (0, -1, -1)
    self.camera_rx_stamp = 0
    self.camera_rx_size = 0
    self.cluster_display_stamp = 0
    self.cluster_display_codes = (-1, -1)
    self.cluster_display_since = (0, 0)

  def update(self, can_packets):
    packets_by_bus = defaultdict(list)
    for stamp, frames in can_packets:
      by_bus = defaultdict(list)
      for address, data, bus in frames:
        if address == PV5_CAMERA_LANE_ADDRESS and bus == self.camera_parser.bus:
          # A duplicate or reordered observation invalidates the sample until a
          # new, strictly later camera frame arrives.
          self.camera_rx_size = len(data) if stamp > self.camera_rx_stamp else 0
          self.camera_rx_stamp = max(stamp, self.camera_rx_stamp)
        bus_addresses = self.addresses.get(bus)
        size = bus_addresses.get(address) if bus_addresses is not None else None
        if size is None:
          continue
        source = (bus, address)
        if stamp <= 0 or stamp < self.last_seen.get(source, 0):
          continue
        self.last_seen[source] = stamp
        if len(data) == size:
          by_bus[bus].append((address, data, bus))
        else:
          self.invalid_dlc[source] = stamp
      for bus, candidates in by_bus.items():
        packets_by_bus[bus].append((stamp, candidates))
    for bus, packets in packets_by_bus.items():
      self.parsers[bus].update(packets)

    self.camera = self._camera_marking()
    newest, left, right = 0, -1, -1
    display_left = display_right = -1
    for bus, parser in self.parsers.items():
      for address in self.addresses[bus]:
        state = parser.message_states[address]
        invalid = max(state.last_invalid_nanos, self.invalid_dlc.get((bus, address), 0))
        if not state.timestamps or state.timestamps[-1] <= invalid:
          continue
        stamp = state.timestamps[-1]
        if stamp < newest:
          continue
        signals = parser.vl[address]
        # The DBC uses zero color or confidence to indicate no detection.
        observed_left, observed_right = (
          int(signals[f'{side}_LANE_TYPE'])
          if signals[f'{side}_LANE_COLOR'] in (1, 2, 3) and signals[f'{side}_LANE_LINE'] in (1, 2, 3)
          else -1
          for side in ('LEFT', 'RIGHT')
        )
        observed_display = (-1, -1)
        if (not state.ignore_checksum and not state.ignore_counter and not state.counter_fail
            and (len(state.timestamps) == 1 or stamp > state.timestamps[-2])):
          observed_display = (_dedicated_display_code(signals, 'LEFT'), _dedicated_display_code(signals, 'RIGHT'))
        if stamp > newest:
          newest, left, right = stamp, observed_left, observed_right
          display_left, display_right = observed_display
        else:
          # Packets batched with the same timestamp have no reliable ordering.
          left = left if left == observed_left else -1
          right = right if right == observed_right else -1
          display_left = display_left if display_left == observed_display[0] else -1
          display_right = display_right if display_right == observed_display[1] else -1
    # Prefer a fresh dedicated marking message, including an explicit solid
    # or unknown side. Use only original camera-bus 0x161 as a fallback.
    now = max(newest, self.camera_parser._last_update_nanos)
    if newest and now - newest <= PV5_LANE_MARKING_MAX_AGE_NS:
      self.display = (newest, display_left, display_right)
      return newest, left, right
    cluster = self._cluster_marking(now)
    self.display = self._cluster_display()
    return cluster

  def _camera_marking(self):
    parser = self.camera_parser
    state = parser.message_states.get(PV5_CAMERA_LANE_ADDRESS)
    stamp = self.camera_rx_stamp
    if (state is None or not state.timestamps or self.camera_rx_size != state.size
        or state.timestamps[-1] != stamp or stamp <= state.last_invalid_nanos
        or state.ignore_checksum or state.ignore_counter or state.counter_fail
        or not 0 <= parser._last_update_nanos - stamp <= PV5_LANE_MARKING_MAX_AGE_NS):
      return 0, -1, -1
    values = parser.vl[PV5_CAMERA_LANE_ADDRESS]
    return stamp, int(values['LANELINE_LEFT']), int(values['LANELINE_RIGHT'])

  def _cluster_display(self):
    stamp, left_code, right_code = self.camera
    if not stamp:
      self.cluster_display_stamp = 0
      return UNKNOWN_DISPLAY

    codes = (left_code, right_code)
    if stamp != self.cluster_display_stamp:
      continuous = 0 < stamp - self.cluster_display_stamp <= CLUSTER_DISPLAY_MAX_GAP_NS
      self.cluster_display_since = tuple(
        prior if continuous and code == old else stamp
        for code, old, prior in zip(codes, self.cluster_display_codes, self.cluster_display_since, strict=True)
      )
      self.cluster_display_codes = codes
      self.cluster_display_stamp = stamp

    # Code 2 tracked solid boundaries in synchronized video across white and
    # yellow paint. Require a stable observation and export no paint color.
    left, right = (
      0 if code == PV5_CLUSTER_DASHED else
      1 if code == PV5_CLUSTER_SOLID_DISPLAY and stamp - since >= CLUSTER_SOLID_CONFIRM_NS else -1
      for code, since in zip(codes, self.cluster_display_since, strict=True)
    )
    return stamp, left, right

  def _cluster_marking(self, now):
    parser = self.camera_parser
    address = 0x161
    state = parser.message_states.get(address)
    if state is None or not state.timestamps or len(parser.dat.get(address, b'')) != state.size:
      return 0, -1, -1
    stamp = state.timestamps[-1]
    if stamp <= state.last_invalid_nanos or not 0 <= now - stamp <= PV5_LANE_MARKING_MAX_AGE_NS:
      return 0, -1, -1
    values = parser.vl[address]
    # Synchronized PV5 CAN/video identifies code 8 as a dashed boundary on
    # either side. The display field does not establish the paint color.
    return (stamp, 0 if values['LANELINE_LEFT'] == PV5_CLUSTER_DASHED else -1,
            0 if values['LANELINE_RIGHT'] == PV5_CLUSTER_DASHED else -1)
