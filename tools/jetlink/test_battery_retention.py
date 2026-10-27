"""Display-only last-good retention under synthetic BLE/USB failures."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/jetlink'), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]
from openpilot.selfdrive.carrot import obd_battery as obd
from openpilot.selfdrive.carrot.cluster.cluster_battery import (
    ARRAYS, METRICS, field_max_age_s, packet_from_metrics, panel_from_wire, panel_labels, remote_panel,
)
from hud_battery import BatteryPublisher


@pytest.mark.parametrize('name', sorted(METRICS))
def test_every_metric_has_power_five_second_or_other_sixty_second_expiry(name):
    value = ('idle' if name == 'thermal_mode' else 'off' if name == 'charge_mode' else
             [3.88] * 108 if name == 'cell_voltages_v' else [0] * 15 if name == 'module_temps_c' else
             420. if name == 'pack_voltage_v' else 1.)
    ttl = 5. if name == 'pack_power_kw' else 60.
    assert field_max_age_s(name) == ttl
    packet = packet_from_metrics({name: (value, 100.)}, 100.)
    assert name in packet_from_metrics({name: (value, 100.)}, 100. + ttl)['fields']
    assert name not in packet_from_metrics({name: (value, 100.)}, 100. + ttl + .01)['fields']
    assert panel_from_wire(packet, ttl).value(name) is not None
    assert panel_from_wire(packet, ttl + .01).value(name) is None


@pytest.fixture
def source(tmp_path, monkeypatch):
    cfg, snap = tmp_path / 'cfg', tmp_path / 'snap'
    config = {'version': 1, 'address': '00:11:22:33:44:55',
              'confirmed_fields': ['pack_power_kw', 'soc_percent', 'max_temp_c']}
    cfg.write_text(json.dumps(config))
    monkeypatch.setattr(obd, 'CONFIG', cfg)
    monkeypatch.setattr(obd, 'SNAPSHOT', snap)
    values = {'pack_power_kw': -5., 'soc_percent': 70., 'max_temp_c': 25.,
              'cell_voltages_v': [3.88] * 108, 'module_temps_c': [20] * 15}
    def write(now, state='receiving', selected=None):
        selected = values if selected is None else selected
        identity = obd.config_id(obd.configuration(config))
        obd.write_snapshot(state, selected if state == 'receiving' else {},
                           frozenset(config['confirmed_fields']), snap, identity,
                           observed_at=now, field_times=dict.fromkeys(selected, now))
    publisher = BatteryPublisher(tmp_path / 'no_passive_profile')
    write(100.)
    publisher.update('KIA_PV5', True, 100.)
    return publisher, write, cfg, snap, config


def test_disconnect_and_setup_do_not_extend_observation_age_or_power_expiry(source):
    publisher, write, _, _, _ = source
    assert 'pack_power_kw' in publisher.update('KIA_PV5', True, 105.)['fields']
    for now, state in ((105.01, 'disconnected'), (110., 'connecting'), (130., 'unavailable'), (159., 'connecting')):
        write(now, state)
        packet = publisher.update('KIA_PV5', True, now)
        assert 'pack_power_kw' not in packet['fields']
        assert packet['fields']['soc_percent'] == {'value': 70., 'age_s': pytest.approx(now - 100.)}
        assert set(packet['unverified_fields']) == {'cell_voltages_v', 'module_temps_c'}
        assert len(publisher.ble_metrics) <= len(METRICS)
        assert all(isinstance(publisher.ble_metrics[name][0], tuple) for name in ARRAYS)
        assert publisher.socket is None and publisher.messaging is None
    assert publisher.update('KIA_PV5', True, 160.)['fields']['soc_percent']['age_s'] == 60.
    assert publisher.update('KIA_PV5', True, 160.01)['fields'] == {}
    assert not publisher.ble_metrics and not publisher.ble_unverified


def test_reconnect_refreshes_only_fields_actually_received(source):
    publisher, write, _, _, _ = source
    write(130., selected={'max_temp_c': 26., 'pack_power_kw': 2.})
    packet = publisher.update('KIA_PV5', True, 130.)
    assert packet['fields']['max_temp_c']['age_s'] == 0.
    assert packet['fields']['soc_percent']['age_s'] == 30.
    write(132., 'unavailable')
    packet = publisher.update('KIA_PV5', True, 160.01)
    assert set(packet['fields']) == {'max_temp_c'}
    assert packet['fields']['max_temp_c']['age_s'] == pytest.approx(30.01)
    assert publisher.update('KIA_PV5', True, 190.01)['fields'] == {}


def test_entering_ble_retires_passive_reports_before_configuration_removal(source):
    publisher, _, cfg, _, _ = source
    publisher.report = packet_from_metrics({'soc_percent': (99., 100.)}, 100.)
    publisher.slow_metrics = {'soc_percent': (99., 100.)}
    packet = publisher.update('KIA_PV5', True, 100.5)
    assert packet['fields']['soc_percent']['value'] == 70.
    assert publisher.report is None and not publisher.slow_metrics
    cfg.unlink()
    assert publisher.update('KIA_PV5', True, 101.)['fields'] == {}


@pytest.mark.parametrize('reset', ['config_change', 'config_invalid', 'config_removed', 'page', 'vehicle'])
def test_retained_values_cannot_cross_configuration_vehicle_or_page(source, reset):
    publisher, write, cfg, _, config = source
    write(101., 'disconnected')
    if reset == 'config_change':
        cfg.write_text(json.dumps(config | {'address': 'AA:BB:CC:DD:EE:FF'}))
    elif reset == 'config_invalid':
        cfg.write_text('{}')
    elif reset == 'config_removed':
        cfg.unlink()
    elif reset == 'page':
        assert publisher.update('KIA_PV5', False, 101.) is None
    elif reset == 'vehicle':
        assert not publisher.update('OTHER', True, 101.)['fields']
    assert not publisher.update('KIA_PV5', True, 102.)['fields']
    assert not publisher.ble_metrics and not publisher.ble_unverified


def test_old_values_are_labeled_and_never_keep_badge_or_usb_transport_alive(source):
    from openpilot.common.obd_status import ObdBadgeStatus
    publisher, write, cfg, snap, _ = source
    write(106., 'unavailable')
    packet = publisher.update('KIA_PV5', True, 106.)
    panel = panel_from_wire(packet)
    assert panel.status == 'unverified'
    assert '이전 수신값 유지' in panel_labels(panel, 'ko')[panel.status]
    assert not ObdBadgeStatus(cfg, snap).update(106., True)
    snapshot = (10., {'sent': 106., 'battery_hud': packet})
    assert remote_panel(snapshot, 10.2).value('soc_percent') == 70.
    assert not remote_panel(snapshot, 10.51).fields


def test_out_of_order_replay_or_clock_rollback_never_renews_last_good(source):
    publisher, write, _, _, _ = source
    write(110., selected={'soc_percent': 71.})
    publisher.update('KIA_PV5', True, 110.)
    write(109., selected={'soc_percent': 69.})
    packet = publisher.update('KIA_PV5', True, 111.)
    assert packet['fields']['soc_percent'] == {'value': 71., 'age_s': 1.}
    assert not publisher.update('KIA_PV5', True, 90.)['fields']
