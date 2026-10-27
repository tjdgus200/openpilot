"""Synthetic sensor data: protocol/indices/freshness, not PV5 calibration."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/jetlink'), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]

from openpilot.selfdrive.carrot import obd_battery as b
from cluster_battery import (charging_view, heat_color, packet_from_metrics,
                             panel_from_wire, valid_value)
from test_obd_battery import payload, response, soc_payload, coolant_payload


def block(command, value=194):
  return bytes.fromhex('62' + command[2:]) + bytes(4) + bytes([value] * 32)


def test_observed_topology_excludes_noncell_tail_and_preserves_holes():
  blocks = {cmd: block(cmd) for cmd, _ in b.CELL_BLOCKS}
  last = bytearray(blocks['22010A'])
  last[19:] = bytes([200] * 20)  # Plausible-looking metadata must never join the array.
  blocks['22010A'] = last
  first = bytearray(blocks['220102'])
  first[7+9] = 0
  blocks['220102'] = first
  values = b.cell_array(blocks)
  assert len(values) == 108 and values[9] is None and values[10] == 3.88
  assert values[-1] == 3.88
  for cmd, _ in b.CELL_BLOCKS:
    assert b.parse_reply(response(bytes(blocks[cmd])), cmd) == bytes(blocks[cmd])
  with pytest.raises(ValueError):
    b.cell_array(blocks | {'22010A': blocks['22010A'][:-1]})
  with pytest.raises(ValueError):
    b.cell_array(blocks | {'220103': block('220104')})
  for unsupported in ('22010B', '22010C'):
    assert unsupported not in b.COMMANDS


def test_temperature_zero_is_real_padding_is_not_a_sensor():
  first, fifth = bytearray(payload()), bytearray(soc_payload())
  first[19:24] = bytes([20, 0, 21, 22, 23])
  fifth[12:19] = bytes([24] * 7)
  fifth[42:46] = bytes([25, 255, 127, 55])
  values = b.temperature_array(first, fifth)
  assert len(values) == 15 and values[1] == 0
  assert values[-3:] == [25, -1, None]
  assert 55 not in values  # Slot16 was outside the observed topology.


@pytest.mark.parametrize('flags,mode', [(0, 'off'), (32, 'off'), (64, 'off'),
                                      (0xA0, 'ac'), (0xC0, 'dc'), (0x80, None), (0xE0, None)])
def test_charge_candidate_never_uses_negative_power_or_plug_alone(flags, mode):
  first = bytearray(payload())  # Already contains negative power.
  first[12] = flags
  result = b.charge_candidate(first)
  assert result == ({'charge_mode': mode} if mode else {})


def test_arrays_are_bounded_immutable_expire_and_unknowns_stay_grey():
  raw = [3.88] * 108
  metrics = {'cell_voltages_v': (raw, 100.), 'module_temps_c': ([0.] * 15, 100.)}
  packet = packet_from_metrics(metrics, 101)
  panel = panel_from_wire(json.loads(json.dumps(packet)))
  assert isinstance(panel.value('cell_voltages_v'), tuple)
  raw[0] = 3.0
  assert panel.value('cell_voltages_v')[0] == 3.88
  assert panel_from_wire(packet, 59.01).value('cell_voltages_v') is None
  for invalid in ([3.88] * 109, [3.88] * 107, [None] * 108, [float('nan')] * 108,
                  [True] * 108, [[3.88]] * 108, [0.] * 108):
    assert not valid_value('cell_voltages_v', invalid)
  assert valid_value('module_temps_c', [0.] * 15)
  assert heat_color(None, 3.88, 'cell_voltages_v') == (85, 90, 100)
  assert heat_color(3.88, 3.88, 'cell_voltages_v') == (55, 205, 115)
  assert heat_color(4., 3.88, 'cell_voltages_v') == (240, 65, 65)
  # Fixed scale: a small spread is not normalized to red in every frame.
  assert heat_color(3.89, 3.88, 'cell_voltages_v') == heat_color(3.88, 3.88, 'cell_voltages_v')
  assert heat_color(29, 21, 'module_temps_c') == (240, 65, 65)


@pytest.mark.parametrize('gear,speed,fresh,expected', [
  ('P', 0., True, True), ('D', 0., True, False), ('P', 1., True, False),
  ('P', 0., False, False), ('P', float('nan'), True, False), ('P', -1., True, False),
])
def test_detail_view_requires_fresh_park_not_charging(gear, speed, fresh, expected):
  for metrics in ({'charge_mode': ('dc', 100.)}, {'charge_mode': ('ac', 100.)},
                  {'charge_mode': ('off', 100.)}, {'pack_power_kw': (-10., 100.)}, {}):
    data = panel_from_wire(packet_from_metrics(metrics, 100.))
    assert charging_view(data, gear, speed, fresh) == expected
  assert not charging_view(None, gear, speed, fresh)


@pytest.mark.parametrize('flags', [0, 0xA0, 0xC0, 0x80])
def test_cell_scans_are_sequential_power_first_only_after_enrollment_park(monkeypatch, flags):
  clock = [100.]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  first = bytearray(payload())
  first[12] = flags  # P enables detail scans even when charge is off/unknown.
  commands = []
  class Link:
    async def command(self, command):
      commands.append(command)
      data = {'220101': first, '220105': soc_payload(), '220106': coolant_payload()}
      return response(data[command] if command in data else block(command))
  async def check():
    p = b.MetricPoller(details=True)
    for i in range(4):
      clock[0] = 100. + i * .5
      start = len(commands)
      await p.poll(Link(), parked=True)
      assert commands[start] == '220101'
      assert sum(c in dict(b.CELL_BLOCKS) for c in commands[start:]) == 1
    assert len(p.values['cell_voltages_v']) == 108 and p.observed['cell_voltages_v'] == 100.
    assert len(p.cell_blocks) == 0
    # Leaving P immediately removes the full array. No extra requests follow.
    start = len(commands)
    await p.poll(Link(), parked=False)
    assert 'cell_voltages_v' not in p.values and not p.cell_blocks
    assert not any(c in dict(b.CELL_BLOCKS) for c in commands[start:])
    assert len(p.values) <= len(b.FIELDS)
  asyncio.run(check())


def test_owner_enabled_preview_is_not_recorded_as_calibration(tmp_path):
  values = {'cell_voltages_v': [3.88] * 108, 'module_temps_c': [20] * 15, 'charge_mode': 'dc'}
  path = tmp_path / 'snapshot'
  b.write_snapshot('receiving', values, path=path, identity='cfg', observed_at=100.)
  record = b.read_json(path)
  packet = b.snapshot_packet(record, 100., 'cfg')
  assert set(packet['fields']) == set(values)
  assert set(packet['unverified_fields']) == set(values)
  assert panel_from_wire(packet).status == 'unverified'
  assert not b.snapshot_packet(record, 106., 'cfg')['fields']
  b.write_snapshot('receiving', values, frozenset(values), path=path, identity='cfg', observed_at=100.)
  packet = b.snapshot_packet(b.read_json(path), 100., 'cfg')
  assert len(packet['fields']['cell_voltages_v']['value']) == 108
  assert 'unverified_fields' not in packet and panel_from_wire(packet).status == 'receiving'
  assert path.stat().st_size < 4096 and len(json.dumps(packet)) < 2048


def test_existing_enrollment_automatically_displays_preview_and_keeps_scalar_gate(tmp_path, monkeypatch):
  from hud_battery import BatteryPublisher
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  cfg, snap = tmp_path / 'cfg', tmp_path / 'snap'
  cfg.write_text(json.dumps(config))
  original = cfg.read_bytes()
  monkeypatch.setattr(b, 'CONFIG', cfg)
  monkeypatch.setattr(b, 'SNAPSHOT', snap)
  now = 100.
  values = {'charge_mode': 'ac', 'module_temps_c': [20]*15, 'cell_voltages_v': [3.88]*108,
            'max_temp_c': 20, 'thermal_mode': 'heating'}
  b.write_snapshot('receiving', values, path=snap, identity=b.config_id(b.configuration(config)), observed_at=now)
  packet = BatteryPublisher(tmp_path/'no_profile').update('KIA_PV5', True, now)
  assert set(packet['fields']) == {'charge_mode', 'module_temps_c', 'cell_voltages_v'}
  panel = panel_from_wire(packet)
  assert panel.status == 'unverified' and charging_view(panel, 'P', 0., True)
  assert cfg.read_bytes() == original and json.loads(original)['confirmed_fields'] == []
  assert not BatteryPublisher(tmp_path/'no_profile').update('OTHER', True, now)['fields']


@pytest.mark.parametrize('pending', [None, 'charge_mode', [['charge_mode']], ['unknown'], ['charge_mode']*6])
def test_bad_preview_provenance_fails_closed(pending):
  record = {'version':1, 'updated':100., 'state':'receiving', 'config_id':'cfg',
            'metrics':{'charge_mode':'ac'}, 'unverified_fields':pending}
  assert not b.snapshot_packet(record,100.,'cfg')['fields']
  if pending != ['charge_mode']*6:
    assert not panel_from_wire({'version':1,'fields':{'charge_mode':{'value':'ac','age_s':0.}},
                                'unverified_fields':pending}).fields


def test_health_renderer_reverts_on_stale_motion_or_gear_change_not_charge_end(monkeypatch):
  import hud_health as h
  import hud_protocol
  from cluster_live import standby_state
  clock = [10.1]
  metrics = {'charge_mode': ('ac', 100.), 'pack_power_kw': (-5., 100.)}
  record = {'sent': 100., 'battery_hud': packet_from_metrics(metrics, 100.),
            'received': {'carState': 99.95}, 'events': {'carState': 'present'},
            'alive': {'carState': True}, 'valid': {'carState': True}}
  monkeypatch.setattr(hud_protocol, 'read_snapshot', lambda: (10., record))
  monkeypatch.setattr(h.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(h, '_fresh', lambda *args: {})
  ui = h.HealthRenderer(screen_mode=8)
  monkeypatch.setattr(h.ClusterUiRenderer, 'render', lambda *args: None)
  parked = replace(standby_state(), gear_text='P', speed_kph=0.)
  ui.render(parked)
  assert ui._charging_dashboard_active
  ui.render(replace(parked, gear_text='D'))
  assert not ui._charging_dashboard_active
  ui.render(replace(parked, speed_kph=3.))
  assert not ui._charging_dashboard_active
  clock[0] = 10.4
  ui.render(parked)
  assert not ui._charging_dashboard_active  # OBD still fresh; motion expired.
  clock[0] = 10.1
  record['battery_hud'] = packet_from_metrics({'charge_mode': ('off', 100.), 'pack_power_kw': (-5.,100.)},100.)
  ui.render(parked)
  assert ui._charging_dashboard_active  # P selects detail; charge end does not hide it.
  ui.render(replace(parked, gear_text='D'))
  assert not ui._charging_dashboard_active  # Negative power cannot select it in D.
  record['battery_hud'] = packet_from_metrics({}, 100.)
  ui.render(parked)
  assert ui._charging_dashboard_active  # Missing battery values remain gray in P.
  ui.screen_mode = 0
  ui.render(parked)
  assert not ui._charging_dashboard_active  # Other screen modes stay unchanged.
  assert not h.vehicle_motion_fresh(None, 10.)
  record['sent'] = 99.
  assert not h.vehicle_motion_fresh((10.,record), 10.1)  # Future-dated motion is rejected.


def test_slow_partial_scan_never_mixes_generations_and_park_is_rechecked(monkeypatch):
  clock, allowed, commands = [100.], [True], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  first = bytearray(payload())
  first[12] = 0xA0
  class Link:
    async def command(self, command):
      commands.append(command)
      data = {'220101': first, '220105': soc_payload(), '220106': coolant_payload()}
      return response(data[command] if command in data else block(command))
  async def check():
    poller = b.MetricPoller(details=True)
    await poller.poll(Link(), parked=lambda: allowed[0])
    assert set(poller.cell_blocks) == {'220102'}
    clock[0] = 103.
    await poller.poll(Link(), parked=lambda: allowed[0])
    assert set(poller.cell_blocks) == {'220102'} and commands[-1] == '220102'
    assert 'cell_voltages_v' not in poller.values
    allowed[0] = False
    await poller.poll(Link(), parked=lambda: allowed[0])
    assert not poller.cell_blocks and 'cell_voltages_v' not in poller.values
    assert not b.MetricPoller(details=True).values  # A reconnect has no old owner/buffers.
  asyncio.run(check())


def test_optional_request_requires_fresh_parked_vehicle():
  class Monitor:
    valid, alive, recv_time = {'carState': True}, {'carState': True}, {'carState': 100.}
    state = NS(vEgo=0., gearShifter='park')
    def __getitem__(self, name):
      return self.state
  monitor = Monitor()
  assert b.parked_state(monitor, 100.1)
  assert not b.parked_state(monitor, 100.31)
  assert not b.parked_state(monitor, 99.)
  monitor.state.vEgo = float('nan')
  assert not b.parked_state(monitor, 100.1)


@pytest.mark.parametrize('mode,title', [('dc', 'DC CHARGING'), ('ac', 'AC CHARGING'),
                                       ('off', 'PARKED BATTERY OVERVIEW'), (None, 'PARKED BATTERY OVERVIEW')])
def test_charging_layout_draws_108_voltage_15_temperature_slots_without_world(monkeypatch, mode, title):
  import cluster_renderer as r
  from cluster_live import standby_state
  renderer = r.ClusterUiRenderer()
  renderer._charging_dashboard_active = True
  arrays = {'cell_voltages_v': ([3.88] * 107 + [None], 100.), 'module_temps_c': ([20] * 15, 100.),
            **({'charge_mode': (mode, 100.)} if mode else {})}
  renderer.language = 'en'
  state = replace(standby_state(), battery=panel_from_wire(packet_from_metrics(arrays, 100.)))
  rectangles, texts = [], []
  monkeypatch.setattr(r.rl, 'draw_rectangle', lambda *args: rectangles.append(args))
  monkeypatch.setattr(renderer, '_rounded_rect', lambda *args: None)
  monkeypatch.setattr(renderer, '_draw_text', lambda text, *args, **kwargs: texts.append(str(text)))
  monkeypatch.setattr(renderer, '_ellipsize_text', lambda text, *args: text)
  renderer._draw_charging_detail(state)
  assert len(rectangles) == 108 + 15
  assert '--' in texts and any('108' in t for t in texts)
  assert title in texts
  calls = []
  monkeypatch.setattr(renderer, '_clear_world', lambda: calls.append('clear'))
  monkeypatch.setattr(renderer, '_render_world', lambda *args: pytest.fail('charging must skip 3D'))
  monkeypatch.setattr(renderer, '_draw_battery_panel', lambda *args: calls.append('battery'))
  monkeypatch.setattr(renderer, '_draw_charging_detail', lambda *args: calls.append('details'))
  monkeypatch.setattr(renderer, '_draw_status_footer', lambda *args: calls.append('footer'))
  monkeypatch.setattr(renderer, '_draw_alert_overlay', lambda *args: calls.append('alerts'))
  monkeypatch.setattr(r.rl, 'rl_push_matrix', lambda: None)
  monkeypatch.setattr(r.rl, 'rl_pop_matrix', lambda: None)
  monkeypatch.setattr(r.rl, 'rl_scalef', lambda *args: None)
  renderer.render(state, (False, False))
  assert calls == ['clear', 'battery', 'details', 'footer', 'alerts']
