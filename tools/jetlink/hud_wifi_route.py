"""Use only the branch-owned, directly connected comma AP for peer data."""
import ipaddress
import json
import subprocess

from wifi_direct_netlink import INTERFACE

DIRECT_SUBNETS = ('172.29.77.0/24', '172.29.78.0/24', '10.203.77.0/24')


def direct_subnet(value):
  address = ipaddress.ip_address(value)
  return next((network for text in DIRECT_SUBNETS
               if address in (network := ipaddress.ip_network(text))
               and address not in (network.network_address, network.broadcast_address)), None)


def receiver_addresses(network):
  # A receiver on a phone/router WLAN must not accept peer display/vision data.
  return [(interface, source) for interface, source in network
          if interface.startswith('wl') and (subnet := direct_subnet(source)) is not None
          and ipaddress.ip_address(source) != subnet.network_address + 1]


def receiver_peer(source, peer):
  subnet = direct_subnet(source)
  return subnet is not None and ipaddress.ip_address(peer) == subnet.network_address + 1


def wlan_address(value):
  address = ipaddress.ip_address(value)
  return address.version == 4 and address.is_private and not (address.is_loopback or address.is_link_local or address.is_unspecified)


def select_link(network, targets, query=None):
  if query is None:
    def query(target, source):
      return json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', target, 'from', source], timeout=1., text=True))
  for interface, source in network:
    subnet = direct_subnet(source)
    if interface != INTERFACE or subnet is None or ipaddress.ip_address(source) != subnet.network_address + 1:
      continue
    for target in targets:
      if direct_subnet(target) != subnet or target == source:
        continue
      try:
        route = query(target, source)
        if (route and route[0].get('dev') == INTERFACE and not route[0].get('gateway')
            and route[0].get('type', 'unicast') == 'unicast'):
          return interface, source, target
      except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        continue
  return None
