"""PV5 display records survive upstream bounded USB assembly unchanged."""
import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'third_party/jetlink'), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]
from openpilot.cereal import log
from cluster_battery import packet_from_metrics, remote_panel
from hud_bsd import warning_sides
from hud_health import power_label, temperature_label
from hud_protocol import encode_snapshot, HUD_MESSAGE
from host_reader import ReadAheadTransport
from test_host_request_queue import Input, envelopes
from jetlink.transport.base import LinkTimeout


@pytest.mark.parametrize('side', [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize('replay', [False, True])
def test_battery_and_bsd_survive_fragmentation_without_indicators(side, replay):
  event = log.Event.new_message(carState={
    'leftBlindspot': side[0], 'rightBlindspot': side[1],
    'leftBlinker': False, 'rightBlinker': False,
  })
  record = dict(version=1, sent=100., received={'carState': 99.95},
                valid={'carState': True}, alive={'carState': True},
                events={'carState': base64.b64encode(event.to_bytes()).decode()},
                battery_hud=packet_from_metrics({'soc_percent': (60., 99.9)}, 100.))
  # Exercise multiple 32KiB envelopes within the existing HUD budget.
  record['pc_fixture_padding'] = 'x' * 70000
  raw = encode_snapshot(record)
  messages = envelopes(HUD_MESSAGE, 8, raw)
  assert len(messages) >= 3
  source = Input([m for msg in messages for m in ((msg, msg) if replay else (msg,))])
  reader = ReadAheadTransport(source)
  try:
    msg = reader.recv(.5)
    assert bytes(msg.payload) == raw
    decoded = json.loads(msg.payload.tobytes() if isinstance(msg.payload, memoryview) else msg.payload)
    with log.Event.from_bytes(base64.b64decode(decoded['events']['carState'])) as item:
      state = NS(left_blindspot=item.carState.leftBlindspot, right_blindspot=item.carState.rightBlindspot)
      assert warning_sides(state, (10., decoded), 10.1) == side
      assert warning_sides(state, (10., decoded), 10.5) == (False, False)
    assert remote_panel((10., decoded), 10.1).value('soc_percent') == 60.
    assert source.drained.wait(.5)
    with pytest.raises(LinkTimeout):
      reader.recv(.02)
  finally:
    reader.close()


def test_local_power_and_temperature_do_not_depend_on_transport_record():
  assert power_label({'power_w': 14.7}) == '14.7 W'
  assert temperature_label({'temp_c': 60., 'thermal_severity': 'ok'})[0] == '60.0°C'
  assert power_label({}) == '-- W'
