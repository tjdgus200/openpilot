"""Optional original PV5 lane-marking observations on the camera and A-CAN buses."""

from collections import defaultdict
import math

from opendbc.can import CANParser
from opendbc.car.hyundai.lane_line_time import PV5_LANE_MARKING_MAX_AGE_NS

LANE_MESSAGES = ('CAM_0x2a4', 'CAM_0x362')
PV5_CLUSTER_DASHED = 8


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

  def update(self, can_packets):
    packets_by_bus = defaultdict(list)
    for stamp, frames in can_packets:
      by_bus = defaultdict(list)
      for address, data, bus in frames:
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

    newest, left, right = 0, -1, -1
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
        if stamp > newest:
          newest, left, right = stamp, observed_left, observed_right
        else:
          # Packets batched with the same timestamp have no reliable ordering.
          left = left if left == observed_left else -1
          right = right if right == observed_right else -1
    # Prefer a fresh dedicated marking message, including an explicit solid
    # or unknown side. Use only original camera-bus 0x161 as a fallback.
    now = max(newest, self.camera_parser._last_update_nanos)
    if newest and now - newest <= PV5_LANE_MARKING_MAX_AGE_NS:
      return newest, left, right
    return self._cluster_marking(now)

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
