"""Direct-only policy and phone-toggle regressions; no radio hardware claims."""
import json
import uuid

import pytest

import hud_wifi_route as route
import wifi_apply
import wifi_protocol as protocol
import wifi_publisher as publisher
from test_wifi import config


def direct_config():
  value = config()
  value.update(wifi_policy='direct_only', direct_subnet='172.29.77.0/24')
  return value


def test_absent_ap_keeps_secret_free_usb_update_control(tmp_path, monkeypatch):
  monkeypatch.setattr(publisher, 'collect', list)
  monkeypatch.setattr(protocol, 'PACKET', tmp_path / 'packet')
  monkeypatch.setattr(protocol, 'CONTROL', tmp_path / 'control')
  value = publisher.provisioning('a' * 64, False)
  value['jetson_release'] = {'synthetic': True}
  protocol.receive(json.dumps(value).encode())
  control = json.loads(protocol.CONTROL.read_text())
  assert control['onroad'] is False and control['jetson_release'] == {'synthetic': True}
  assert set(control) == {'onroad', 'jetson_release', 'received'}


def test_phone_disconnect_reconnect_never_changes_export_or_reads_uplink_secrets(monkeypatch):
  ap = '22345678-1234-1234-1234-123456789abc'
  phone = config()['profiles'][0]['id']
  state = [phone, ap]
  monkeypatch.setattr(publisher, 'direct_profile_id', lambda: ap)
  monkeypatch.setattr(publisher, 'direct_client_id', lambda uid: uid)
  def nm(*args):
    if args[-1] == '--active':
      return '\n'.join(state)
    assert args[-1] == ap  # Phone profiles/secrets must never be queried.
    if args[1] == 'connection.interface-name':
      return 'wljetson0'
    if args[1] == 'ipv4.addresses':
      return '172.29.77.1/24'
    assert '--show-secrets' in args
    return 'Carrot-Jetson-12345678\nap\nno\nwpa-psk\nsynthetic AP password'
  monkeypatch.setattr(publisher, 'nm', nm)
  connected = publisher.provisioning('a' * 64, False)
  state[:] = [ap]
  assert publisher.provisioning('a' * 64, False) == connected
  state[:] = [ap, phone]
  assert publisher.provisioning('a' * 64, False) == connected
  assert len(connected['profiles']) == 1 and connected['wifi_policy'] == 'direct_only'
  state[:] = [phone]
  empty = publisher.provisioning('a' * 64, False)
  assert empty['profiles'] == [] and empty['direct_subnet'] is None


@pytest.mark.parametrize('changes', [
  {'wifi_policy': 'shared'}, {'direct_subnet': '10.164.54.0/24'},
  {'direct_subnet': '172.29.77.0/16'},
])
def test_invalid_direct_policy_is_rejected(changes):
  value = direct_config()
  value.update(changes)
  with pytest.raises(ValueError):
    protocol.validate(value)


def test_direct_policy_cannot_include_fallback_profiles():
  value = direct_config()
  value['profiles'].append(dict(value['profiles'][0], id=str(uuid.uuid4())))
  with pytest.raises(ValueError):
    protocol.validate(value)


def test_direct_policy_works_with_immutable_legacy_network_importer(tmp_path):
  # Existing protected worker can import the sole direct profile; once
  # verified, its prior managed hotspot fallback is removed transactionally.
  wifi_apply.install(config('b'), tmp_path, lambda *a: '')
  value = direct_config()
  uid, _ = wifi_apply.render(value['owner'], value['profiles'][0], 100)
  active = []
  calls = []
  def nm(*args):
    calls.append(args)
    if args[-1] == 'status':
      return 'wlp1s0:wifi:connected' if active else 'wlp1s0:wifi:disconnected'
    if args[-1] == '--active':
      return '\n'.join(active)
    if args[:4] == ('--wait', '12', 'connection', 'up'):
      active[:] = [args[-1]]
    return ''
  worker = wifi_apply.Worker(tmp_path, nm)
  assert worker.step((0, value), 0) == 'connected'
  assert wifi_apply.saved_ids(tmp_path) == [uid]
  before = len(calls)
  assert worker.step((30, value), 30) == 'connected'
  assert not any(c[:4] == ('--wait', '12', 'connection', 'up') for c in calls[before:])


@pytest.mark.parametrize('dev,gateway,kind', [('wlan0', None, 'unicast'), ('usb0', None, 'unicast'),
  ('wljetson0', '172.29.77.254', 'unicast'), ('wljetson0', None, 'local')])
def test_wrong_interface_routed_gateway_and_local_routes_are_rejected(dev, gateway, kind):
  rows = [{'dev': dev, 'gateway': gateway, 'type': kind}]
  assert route.select_link([('wljetson0', '172.29.77.1')], ['172.29.77.2'], lambda *_: rows) is None


@pytest.mark.parametrize('source,target', [('172.29.77.2','172.29.77.3'), ('172.29.77.1','172.29.78.2'),
  ('172.29.77.1','172.29.77.255'), ('172.29.77.1','10.164.54.121')])
def test_only_own_ap_gateway_and_same_private_subnet_are_allowed(source, target):
  assert route.select_link([('wljetson0', source)], [target], lambda *_: [{'dev':'wljetson0'}]) is None


def test_direct_route_is_unchanged_when_phone_goes_away_and_returns():
  ap = [('wljetson0', '172.29.77.1')]
  def query(*_):
    return [{'dev':'wljetson0'}]
  expected = ('wljetson0', '172.29.77.1', '172.29.77.2')
  for network in (ap + [('wlan0','10.164.54.41')], ap, [('wlan0','10.1.1.41')] + ap):
    assert route.select_link(network, ['10.164.54.121','172.29.77.2'], query) == expected


def test_receiver_binds_only_usb_identified_active_ap_profile_not_phone():
  value = direct_config()
  uid, _ = wifi_apply.render(value['owner'], value['profiles'][0], 100)
  ap = [('wlp1s0', '172.29.77.2')]
  def query():
    return uid + ':wlp1s0\n' + str(uuid.uuid4()) + ':wlan1'
  expected = tuple(ap)
  for network in (ap, ap + [('wlan1', '10.164.54.121')], [('wlan1','10.1.1.121')] + ap):
    assert route.receiver_network(network, (0, value), query) == expected
  assert route.receiver_network(ap, (0, value), lambda: str(uuid.uuid4()) + ':wlp1s0') == ()
  assert route.receiver_network(ap, (0, config()), query) == ()
  assert route.receiver_network(ap, (0, dict(value, profiles=[])), query) == ()


def test_receiver_requires_fresh_private_provisioning_and_direct_gateway(monkeypatch):
  monkeypatch.setattr(route, 'read_packet', lambda: None)
  assert route.receiver_network([('wlp1s0','172.29.77.2')]) == ()
  assert route.direct_peer('172.29.77.1', '172.29.77.2')
  assert not route.direct_peer('10.164.54.41', '172.29.77.2')
  assert not route.direct_peer('172.29.78.1', '172.29.77.2')


def test_phone_loss_does_not_close_active_ap_even_if_internet_sharing_fails(monkeypatch):
  import wifi_direct
  from unittest.mock import Mock  # noqa: TID251
  wireless = Mock()
  wireless.interfaces.return_value = [{'name':'wljetson0','index':4}]
  wireless.power_save.return_value = 0
  ap = wifi_direct.AccessPoint(wireless, command=lambda *_: 'synthetic-ap-id')
  ap.value, ap.index = {'uuid':'synthetic-ap-id','subnet':'172.29.77.0/24'}, 4
  ap.sharing = Mock()
  ap.sharing.ensure.side_effect = RuntimeError('Internet unavailable')
  monkeypatch.setattr(wifi_direct, 'offroad', lambda: True)
  for _ in range(3):
    assert ap.active()
    ap.awake()
    ap.share_internet()
  assert ap.sharing_state == 'unavailable' and ap.index == 4
  wireless.delete.assert_not_called()
  ap.sharing.close.assert_not_called()
