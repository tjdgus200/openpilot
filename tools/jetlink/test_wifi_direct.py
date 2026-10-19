import importlib.util
import json
from pathlib import Path
import struct
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from wifi_direct_netlink import INTERFACE, attribute, attributes, concurrent_station_ap
from hud_wifi_route import select_link
import wifi_publisher as sender


def number(kind, value):
  return attribute(kind, struct.pack('I', value))


def combinations(limit=2):
  types = attribute(2, b'') + attribute(3, b'')
  limits = attribute(1, number(1, limit) + attribute(2, types))
  combination = attribute(1, limits) + number(2, 2)
  return [{120: attribute(1, combination)}]


def test_modes_alone_do_not_authorize_concurrent_ap():
  assert not concurrent_station_ap([{32: attribute(2, b'') + attribute(3, b'')}])
  assert not concurrent_station_ap(combinations(limit=1))
  assert concurrent_station_ap(combinations())
  with pytest.raises(ValueError):
    attributes(b'\x03\0\x01\0')


def test_created_interface_is_owned_even_if_mode_changes_during_registration():
  from wifi_direct_netlink import Wireless
  w = object.__new__(Wireless)
  w.family = 42
  station = {'name': 'wlan0', 'index': 11, 'type': 2, 'phy': 0}
  observed = iter([[station], [station, {'name': INTERFACE, 'index': 35, 'type': 2, 'phy': 0}]])
  w.interfaces = lambda: next(observed)
  w.query = lambda *a: [{1: struct.pack('I', 0), **combinations()[0]}] if a[1] == 1 else []
  assert w.create() == 35


def test_direct_route_survives_phone_loss_without_reconnect():
  direct = (INTERFACE, '172.29.77.1')
  phone = ('wlan0', '10.164.54.41')
  target = '172.29.77.42'
  def query(peer, source):
    return [{'dev': INTERFACE if peer.startswith('172.29.77.') else 'wlan0'}]
  selected = select_link([phone, direct], [target], query)
  assert selected == (*direct, target)
  assert select_link([direct], [target], query) == selected
  assert select_link([direct, phone], [target], query) == selected


def test_route_selection_never_uses_usb_loopback_or_wrong_interface():
  for device in ('usb0', 'eth0', 'wlan9'):
    assert select_link([('wlan0', '10.1.1.2')], ['10.1.1.3'], lambda *_: [{'dev': device}]) is None
  assert select_link([('usb0', '10.1.1.2')], ['10.1.1.3'], lambda *_: pytest.fail('USB route queried')) is None
  assert select_link([('wlan0', '127.0.0.1')], ['10.1.1.3'], lambda *_: []) is None
  assert select_link([('wlan0', '10.1.1.2')], ['127.0.0.1'], lambda *_: []) is None
  assert select_link([('wlan0', '10.1.1.2')], ['10.1.1.3'], lambda *_: [{'dev': 'wlan0', 'type': 'blackhole'}]) is None


def test_only_owned_active_ap_is_provisioned_and_phone_state_is_irrelevant(monkeypatch):
  direct, phone, other = ['12345678-1234-1234-1234-123456789ab' + n for n in 'cde']
  active = [direct, phone]
  def nm(*args):
    if args[-1] == '--active':
      return '\n'.join(active)
    if args[-1] == 'show':
      return '\n'.join(p + ':802-11-wireless' for p in (other, phone, direct))
    if args[1:2] == ('connection.interface-name',):
      return INTERFACE
    uid = args[-1]
    return ('phone\ninfrastructure' if uid == phone else 'syntheticAP\nap') + '\nno\nwpa-psk\nsynthetic password'
  monkeypatch.setattr(sender, 'nm', nm)
  monkeypatch.setattr(sender, 'direct_profile_id', lambda: direct)
  monkeypatch.setattr(sender, 'direct_client_id', lambda uid: uid)
  before = sender.collect()
  assert [p['id'] for p in before] == [direct]
  assert [p['active'] for p in before] == [True]
  active.remove(phone)
  assert sender.collect() == before
  active.clear()
  assert sender.collect() == []


def test_only_ap_recreation_or_boot_changes_legacy_client_identity(monkeypatch):
  values = {'boot_id': '12345678-1234-1234-1234-123456789abc', 'ifindex': '35'}
  monkeypatch.setattr(Path, 'read_text', lambda p, *a, **k: values[p.name])
  uid = '12345678-1234-1234-1234-123456789abd'
  first = sender.direct_client_id(uid)
  assert sender.direct_client_id(uid) == first
  values['ifindex'] = '36'
  second = sender.direct_client_id(uid)
  assert second != first
  values['boot_id'] = '12345678-1234-1234-1234-123456789abe'
  assert sender.direct_client_id(uid) not in (first, second)


def test_coexistence_hint_fails_closed_for_stale_or_foreign_status(tmp_path, monkeypatch):
  from openpilot.common import wifi_coexistence as coexistence
  path = tmp_path/'status.json'
  monkeypatch.setattr(coexistence, 'STATUS', path)
  monkeypatch.setattr(coexistence.time, 'monotonic', lambda: 100.)
  assert not coexistence.direct_ap_active()
  for state, interface, updated, expected in [('active','wljetson0',99.,True),('active','wljetson0',80.,False),('active','wljetson0',101.,False),('unavailable','wljetson0',99.,False),('active','wlan0',99.,False)]:
    path.write_text(json.dumps(dict(state=state,interface=interface,updated=updated)))
    assert coexistence.direct_ap_active() is expected


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_ap_start_refuses_onroad_before_any_network_operation(monkeypatch):
  import wifi_direct as direct
  wireless = Mock()
  command = Mock()
  monkeypatch.setattr(direct, 'offroad', lambda: False)
  monkeypatch.setattr(direct, 'configuration', lambda: pytest.fail('identity read onroad'))
  ap = direct.AccessPoint(wireless, command)
  assert not ap.start()
  wireless.create.assert_not_called()
  command.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_ap_control_does_not_inherit_usb_big_core_placement(monkeypatch):
  import wifi_direct as direct
  seen = []
  monkeypatch.setattr(direct.os, 'setpriority', lambda *args: None)
  monkeypatch.setattr(direct.os, 'sched_setscheduler', lambda *args: None)
  monkeypatch.setattr(direct.os, 'cpu_count', lambda: 8)
  monkeypatch.setattr(direct.os, 'sched_getaffinity', lambda _: {7})
  monkeypatch.setattr(direct.os, 'sched_setaffinity', lambda pid, mask: seen.append(mask))
  direct.low_priority()
  assert seen == [{0, 1, 2, 3}]


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_partial_activation_can_remove_only_its_owned_interface(tmp_path, monkeypatch):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'offroad', lambda: True)
  monkeypatch.setattr(direct, 'configuration', lambda: dict(uuid='synthetic', subnet='172.29.77.0/24', ssid='synthetic', password='synthetic'))
  monkeypatch.setattr(direct, 'channel', lambda: ('a', 36))
  monkeypatch.setattr(direct.subprocess, 'check_output', lambda *a, **k: '[]')
  monkeypatch.setattr(direct, 'PROFILE', tmp_path/'ap.nmconnection')
  wireless = Mock()
  wireless.create.return_value = 41
  def command(*args):
    if 'up' in args:
      raise RuntimeError('activation rejected')
    return '30 (disconnected)'
  ap = direct.AccessPoint(wireless, command)
  with pytest.raises(RuntimeError):
    ap.start()
  ap.close()
  ap.close()
  wireless.delete.assert_called_once_with(41)
  profile = (tmp_path/'ap.nmconnection').read_text()
  assert 'interface-name=wljetson0' in profile and 'never-default=true' in profile


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_subnet_avoids_existing_wifi_and_vpn_routes():
  import wifi_direct as direct
  assert direct.choose_subnet([{'dst': 'default'}, {'dst': '172.29.77.0/24', 'dev': 'wlan0'}]) == '172.29.78.0/24'
  assert direct.choose_subnet([{'dst': '172.16.0.0/12', 'dev': 'tun0'}]) == '10.203.77.0/24'
  with pytest.raises(StopIteration):
    direct.choose_subnet([{'dst': '172.16.0.0/12'}, {'dst': '10.0.0.0/8'}])


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_ap_power_policy_never_changes_internet_station_and_restores_on_close():
  import wifi_direct as direct
  wireless = Mock()
  wireless.power_save.return_value = 1
  wireless.interfaces.return_value = [{'name': INTERFACE, 'index': 41}, {'name': 'wlan0', 'index': 11}]
  ap = direct.AccessPoint(wireless, Mock())
  ap.index = 41
  ap.previous_power_save = 1
  ap.awake()
  wireless.set_power_save.assert_called_once_with(41, 0)
  ap.close()
  assert [call.args for call in wireless.set_power_save.call_args_list] == [(41, 0), (41, 1)]
  wireless.delete.assert_called_once_with(41)


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_power_restore_does_not_touch_replaced_interface():
  import wifi_direct as direct
  wireless = Mock()
  wireless.interfaces.return_value = [{'name': INTERFACE, 'index': 42}]
  ap = direct.AccessPoint(wireless, Mock())
  ap.index = 41
  ap.previous_power_save = 1
  ap.close()
  wireless.set_power_save.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_failed_power_restore_still_cleans_owned_ap():
  import wifi_direct as direct
  wireless = Mock()
  wireless.interfaces.return_value = [{'name': INTERFACE, 'index': 41}]
  wireless.set_power_save.side_effect = OSError('device disappeared')
  ap = direct.AccessPoint(wireless, Mock())
  ap.index = 41
  ap.previous_power_save = 1
  ap.close()
  assert ap.previous_power_save is None
  wireless.delete.assert_called_once_with(41)


def test_direct_only_never_falls_back_to_phone_or_gateway():
  direct = (INTERFACE, '172.29.77.1')
  phone = ('wlan0', '10.164.54.41')
  assert select_link([phone], ['10.164.54.121'], lambda *_: pytest.fail('phone queried')) is None
  assert select_link([direct, phone], ['10.164.54.121'], lambda *_: pytest.fail('fallback queried')) is None
  assert select_link([direct], ['172.29.78.42'], lambda *_: pytest.fail('other subnet queried')) is None
  assert select_link([direct], ['172.29.77.42'], lambda *_: [{'dev':INTERFACE,'gateway':'172.29.77.2'}]) is None
  assert select_link([direct], ['172.29.77.42'], lambda *_: [{'dev':'wlan0'}]) is None


def test_receiver_only_listens_on_direct_network_and_accepts_comma_gateway():
  from hud_wifi_route import receiver_addresses, receiver_peer
  assert receiver_addresses([('wlP1p1s0','10.164.54.121'),('eth0','172.29.77.42')]) == []
  assert receiver_addresses([('wlP1p1s0','172.29.77.42')]) == [('wlP1p1s0','172.29.77.42')]
  assert receiver_peer('172.29.77.42','172.29.77.1')
  assert not receiver_peer('172.29.77.42','172.29.77.3')
  assert not receiver_peer('172.29.77.42','10.164.54.41')
  assert receiver_peer('10.203.77.42','10.203.77.1')


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
@pytest.mark.parametrize('cached, expected', [('*:5745 MHz', ('a',149)), ('*:5180', ('a',36)),
                                            ('*:2412 MHz', ('bg',1)), ('*:2462 MHz', ('bg',11)),
                                            (':5745 MHz', None)])
def test_actual_nmcli_frequency_units_and_no_station(monkeypatch, cached, expected):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'nm', lambda *args: cached)
  assert direct.station_channel() == expected


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
@pytest.mark.parametrize('cached', ['*:unknown', '*:5500 MHz', '*:5181 MHz'])
def test_active_station_parse_failure_never_silently_selects_channel36(monkeypatch, cached):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'nm', lambda *args: cached)
  with pytest.raises(ValueError):
    direct.channel()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_phone_loss_keeps_existing_ap_channel_and_returned_station_requests_alignment(monkeypatch):
  import wifi_direct as direct
  ap = direct.AccessPoint(Mock(), Mock())
  ap.band_channel = ('a',149)
  monkeypatch.setattr(direct, 'station_channel', lambda: None)
  assert not ap.needs_rechannel()
  monkeypatch.setattr(direct, 'station_channel', lambda: ('a',149))
  assert not ap.needs_rechannel()
  monkeypatch.setattr(direct, 'station_channel', lambda: ('bg',1))
  assert ap.needs_rechannel()
