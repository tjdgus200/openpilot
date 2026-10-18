"""PC-only fixtures use invented signals; they are not a PV5 PID specification."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).parent), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]
import hud_battery as b
import hud_protocol
from cluster_battery import BatteryPanelData, finite_number, packet_from_metrics, panel_from_wire, remote_panel
from cluster_config import CLUSTER_SCREEN_MODE_BATTERY, normalize_cluster_screen_mode
from cluster_live import standby_state
import cluster_renderer as renderer


def signal(name='max_temp_c', **kwargs):
    # All addresses, identifiers and offsets below are synthetic.
    return {'name': name, 'bus': 1, 'response_id': 0x612, 'did': 0xABCD,
            'offset': 0, 'size': 1, 'signed': True, **kwargs}


def profile(signals=None, **kwargs):
    return {'version': 1, 'validated': True, 'vehicle': 'KIA_PV5',
            'validation_reference': 'PC fixture only, not a vehicle profile',
            'signals': signals or [signal()], **kwargs}


def event(data, stamp=10.0, bus=1, address=0x612, valid=True):
    return NS(valid=valid, logMonoTime=int(stamp * 1e9), can=[NS(src=bus, address=address, dat=data)])


def single(payload):
    return bytes([len(payload)]) + payload


@pytest.mark.parametrize('change', [
    {'validated': False}, {'version': True}, {'vehicle': 'OTHER'}, {'validation_reference': ''},
    {'signals': [signal(name=['bad'])]}, {'signals': [signal(size=True)]},
    {'signals': [signal(offset=-1)]}, {'signals': [signal(bus=128)]},
    {'signals': [signal(scale=float('nan'))]}, {'signals': [signal(), signal()]},
    {'signals': [signal(endian='little')]}, {'signals': [signal(scale=0)]},
    {'signals': [signal(mask=255)]}, {'signals': [signal(signed=False, mask=256)]},
    {'validation_reference': '   '},
    {'signals': [signal(invalid_raw=[256])]}, {'signals': [signal(invalid_raw=[True])]},
])
def test_unverified_or_malformed_profiles_fail_closed(change):
    assert b.validate_profile(profile(**change), 'KIA_PV5') is None


def test_wrong_vehicle_and_no_profile_never_open_can(tmp_path):
    source = b.BatteryPublisher(tmp_path / 'missing')
    assert source.update('KIA_PV5', True, 10)['status'] == 'profile_required'
    assert source.socket is None and source.messaging is None
    path = tmp_path / 'profile'
    path.write_text(json.dumps(profile()))
    source = b.BatteryPublisher(path)
    assert source.update('OTHER', True, 10)['fields'] == {}
    assert source.socket is None
    assert source.update('KIA_PV5', False, 11) is None


def test_passive_socket_is_bounded_and_retired_on_page_or_vehicle_change(tmp_path, monkeypatch):
    import openpilot.cereal
    queue = [event(single(b'\x62\xab\xcd\x20')) for _ in range(100)]
    opened, received = [], []
    def sub_sock(name, **kwargs):
        opened.append((name, kwargs))
        return object()
    def receive(sock):
        received.append(sock)
        return queue.pop(0) if queue else None
    # No publisher/send function exists on this stub. The production path
    # must operate through the receive-only API, not a mocked transmit API.
    api = NS(sub_sock=sub_sock, recv_one_or_none=receive)
    monkeypatch.setattr(openpilot.cereal, 'messaging', api, raising=False)
    monkeypatch.setattr(b.time, 'monotonic', lambda: 10.0)
    path = tmp_path / 'profile'
    raw = json.dumps(profile())
    path.write_text(raw)
    source = b.BatteryPublisher(path)
    assert source.update('KIA_PV5', True, 10)['fields']['max_temp_c']['value'] == 32
    assert opened == [('can', {'conflate': False, 'timeout': 0})]
    assert len(received) == 16
    assert path.read_text() == raw
    assert source.update('OTHER', True, 10.1)['fields'] == {}
    assert source.socket is None and source.decoder is None
    assert source.update('OTHER', False, 10.2) is None
    assert source.report is None


def test_thermal_power_does_not_infer_cooling_or_heating():
    decoder = b.PassiveDecoder([signal('thermal_power_kw', signed=False, scale=0.1)])
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd\x18', 10)
    panel = panel_from_wire(decoder.packet(10))
    assert panel.value('thermal_power_kw') == pytest.approx(2.4)
    assert panel.value('thermal_mode') is None


def test_explicit_thermal_mode_bitfield_and_short_payload_clear_old_values():
    mapping = signal('thermal_mode', signed=False, mask=0x30, shift=4,
                     modes={'0': 'idle', '1': 'cooling', '2': 'heating', '3': 'mixed'})
    assert b.validate_profile(profile([mapping]), 'KIA_PV5')
    decoder = b.PassiveDecoder([mapping])
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd\x91', 10)
    assert decoder.metrics['thermal_mode'] == ('cooling', 10)
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd', 11)
    assert 'thermal_mode' not in decoder.metrics


def test_profile_specific_invalid_sensor_sentinel_does_not_become_a_temperature():
    decoder = b.PassiveDecoder([signal(invalid_raw=[127])])
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd\x20', 10)
    assert decoder.metrics['max_temp_c'][0] == 32
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd\x7f', 11)
    assert panel_from_wire(decoder.packet(11)).value('max_temp_c') is None


def test_only_fresh_valid_received_replies_match_a_validated_profile():
    decoder = b.PassiveDecoder(b.validate_profile(profile(), 'KIA_PV5'))
    raw = single(b'\x62\xab\xcd\xfe')
    for sample in (event(raw, bus=0), event(raw, bus=129), event(raw, valid=False),
                   event(raw, stamp=8), event(raw, stamp=11),
                   event(single(b'\x7f\x22\x31')), event(single(b'\x62\xab\xce\x20'))):
        decoder.consume(sample, 10)
    assert not decoder.metrics
    decoder.consume(event(raw), 10)
    assert decoder.metrics['max_temp_c'] == (-2, 10)
    assert panel_from_wire(decoder.packet(10)).value('max_temp_c') == -2
    assert panel_from_wire(decoder.packet(15.01)).value('max_temp_c') is None


def test_can_fd_extended_single_frame():
    decoder = b.PassiveDecoder([signal()])
    decoder.feed(1, 0x612, b'\x00\x04\x62\xab\xcd\x1e' + b'\x00' * 10, 10)
    assert decoder.metrics['max_temp_c'][0] == 30


def test_multiframe_sequence_length_and_timeout():
    decoder = b.PassiveDecoder([signal(offset=8)])
    payload = b'\x62\xab\xcd' + b'\x00' * 8 + b'\x20'
    first = bytes([0x10, len(payload)]) + payload[:6]
    last = b'\x21' + payload[6:]
    decoder.feed(1, 0x612, first, 10)
    decoder.feed(1, 0x612, b'\x22' + payload[6:], 10.1)
    assert not decoder.metrics and not decoder.pending
    decoder.feed(1, 0x612, first, 10)
    decoder.feed(1, 0x612, last, 11.1)
    assert not decoder.metrics
    decoder.feed(1, 0x612, first, 12)
    decoder.feed(1, 0x612, last, 12.1)
    assert decoder.metrics['max_temp_c'] == (32, 12.1)
    decoder.feed(1, 0x612, b'\x1f\xff\x62\xab\xcd\x00\x00\x00', 13)
    assert not decoder.pending


def test_derived_power_and_cell_spread_require_coherent_inputs():
    signals = [signal('pack_voltage_v', size=2, signed=False),
               signal('pack_current_a', offset=2, size=2, scale=0.1),
               signal('max_cell_v', offset=4, signed=False, scale=0.02),
               signal('min_cell_v', offset=5, signed=False, scale=0.02)]
    decoder = b.PassiveDecoder(signals)
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd\x01\x90\xff\x9c\xc8\xc7', 10)
    packet = decoder.packet(10)
    assert packet['fields']['pack_power_kw']['value'] == -4
    assert packet['fields']['cell_delta_mv']['value'] == pytest.approx(20)
    assert not (b.SOURCE_LIMITS.keys() & packet['fields'].keys())
    decoder.metrics['pack_current_a'] = (10, 11)
    decoder.decode_payload(1, 0x612, b'\x62\xab\xcd', 11)
    assert 'pack_power_kw' not in decoder.metrics


def test_temperature_trend_needs_five_minutes_of_continuous_fresh_data():
    decoder = b.PassiveDecoder([signal()])
    for stamp in range(1000):
        decoder.metrics['max_temp_c'] = 20 + stamp * 0.01, float(stamp)
        result = decoder.packet(stamp)
        if stamp < 298:
            assert 'temp_change_5min_c' not in result['fields']
        assert len(decoder.history) <= 302
    assert result['fields']['temp_change_5min_c']['value'] == pytest.approx(3.01)
    decoder.metrics['max_temp_c'] = 31, 1006
    assert 'temp_change_5min_c' not in decoder.packet(1006)['fields']
    assert 'temp_change_5min_c' not in decoder.packet(1012)['fields']
    assert not decoder.history


def test_inconsistent_temperature_range_discards_the_trend_history():
    decoder = b.PassiveDecoder([signal()])
    decoder.metrics = {'max_temp_c': (30, 10), 'min_temp_c': (31, 10)}
    assert decoder.packet(10)['fields'] == {}
    assert not decoder.history


def test_each_field_expires_independently_and_zero_is_not_missing():
    record = packet_from_metrics({'soc_percent': (0, 10), 'max_temp_c': (20, 6),
                                  'thermal_mode': ('idle', 10), 'thermal_power_kw': (0, 10)}, 10)
    panel = panel_from_wire(record, 2)
    assert panel.value('soc_percent') == 0
    assert panel.value('thermal_power_kw') == 0
    assert panel.value('thermal_mode') == 'idle'
    assert panel.value('max_temp_c') is None
    assert panel_from_wire({'version': True, 'fields': {}}).fields == ()
    assert not finite_number(10 ** 1000)


@pytest.mark.parametrize('value', [True, '32', float('nan'), float('inf'), 150, None])
def test_bad_temperature_is_never_formatted_as_real(value):
    record = {'version': 1, 'fields': {'max_temp_c': {'value': value, 'age_s': 0}}}
    assert panel_from_wire(record).value('max_temp_c') is None


def test_remote_ages_do_not_require_synchronized_clocks_or_reset_each_snapshot():
    record = packet_from_metrics({'max_temp_c': (32, 99999)}, 100000)
    snapshot = 10.0, {'sent': 100001.0, 'battery_hud': record}
    panel = remote_panel(snapshot, 10.2)
    assert panel.value('max_temp_c') == 32
    assert panel.oldest_age_s == pytest.approx(2.2)
    assert remote_panel(snapshot, 10.51).fields == ()
    record['sampled_at'] = 100002
    assert remote_panel(snapshot, 10.2).fields == ()


def test_optional_source_failure_does_not_break_other_snapshots():
    builder = object.__new__(hud_protocol.SnapshotBuilder)
    builder.params = NS(get_int=lambda key: 8)
    builder.sm = {'carParams': NS(carFingerprint='KIA_PV5')}
    builder.battery_retry_at = 0
    calls = []
    class Broken:
        def update(self, *args):
            raise OSError('private details must not be forwarded')
        def close(self):
            calls.append('close')
    builder.battery = Broken()
    result = builder.battery_snapshot(10)
    assert result == {'version': 1, 'sampled_at': 10, 'status': 'waiting', 'fields': {}}
    assert calls == ['close'] and builder.battery is None and builder.battery_retry_at == 15
    assert builder.battery_snapshot(11)['fields'] == {}
    builder.params = NS(get_int=lambda key: 0)
    assert builder.battery_snapshot(12) is None


def test_battery_layout_has_no_removed_fields_and_preserves_alert_order(monkeypatch):
    ui = object.__new__(renderer.ClusterUiRenderer)
    ui.language, ui.panel_layout, ui.screen_mode = 'ko', 0, CLUSTER_SCREEN_MODE_BATTERY
    ui.theme_mode = 1
    ui.width, ui.height = 1920, 480
    texts = []
    ui._rounded_rect = lambda *a: None
    ui._ellipsize_text = lambda text, *a: text
    ui._draw_text = lambda text, *a, **kw: texts.append(text)
    monkeypatch.setattr(renderer.rl, 'draw_line_ex', lambda *a: None)
    ui._draw_battery_panel(standby_state())
    assert '배터리 종합' in texts and '--%' in texts and '-- · -- kW' in texts
    assert not any(word in text for word in ('SOH', '팩 전압', '보조배터리', '전류') for text in texts)
    assert '배터리 데이터 수신 대기' in texts
    calls = []
    ui._profile_start = lambda: 0
    ui._profile_add = lambda *a: None
    ui._render_world = lambda *a: calls.append('world')
    ui._draw_hud = lambda *a: calls.append('hud')
    ui._draw_alert_overlay = lambda *a: calls.append('vehicle_alert')
    ui.render(standby_state(), (False, False))
    assert calls == ['world', 'hud', 'vehicle_alert']


def test_mode_selection_defaults_and_valid_zero_rendering():
    assert normalize_cluster_screen_mode('battery') == normalize_cluster_screen_mode(8) == 8
    assert normalize_cluster_screen_mode(9) == normalize_cluster_screen_mode(None) == 0
    data = panel_from_wire(packet_from_metrics({'soc_percent': (0, 10), 'max_temp_c': (0, 10)}, 10))
    state = replace(standby_state(), battery=data)
    assert state.battery.value('soc_percent') == 0
    assert isinstance(state.battery, BatteryPanelData)


def test_battery_update_does_not_invalidate_the_driving_scene_cache():
    from cluster_scene import cluster_scene_state_key
    state = standby_state()
    battery = panel_from_wire(packet_from_metrics({'max_temp_c': (32, 10)}, 10))
    assert cluster_scene_state_key(state) == cluster_scene_state_key(replace(state, battery=battery))
