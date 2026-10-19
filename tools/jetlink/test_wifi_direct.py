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
  before = sender.collect()
  assert [p['id'] for p in before] == [direct, phone]
  assert [p['active'] for p in before] == [True, False]
  active.remove(phone)
  assert sender.collect() == before
  active.clear()
  assert [p['id'] for p in sender.collect()] == [phone]


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
