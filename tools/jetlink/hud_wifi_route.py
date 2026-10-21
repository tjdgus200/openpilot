"""Private comma AP only; the phone station is never a peer-data fallback."""
import ipaddress
import json
import subprocess

from wifi_direct_netlink import INTERFACE
from wifi_protocol import DIRECT_SUBNETS, read_packet


def wlan_address(value):
  address = ipaddress.ip_address(value)
  return address.version == 4 and address.is_private and not (address.is_loopback or address.is_link_local or address.is_unspecified)


def select_link(network, targets, query=None):
  if query is None:
    def query(target, source):
      return json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', target, 'from', source], timeout=1., text=True))
  for interface, source in network:
    if interface != INTERFACE or not wlan_address(source):
      continue
    subnet = next((ipaddress.ip_network(s) for s in DIRECT_SUBNETS
                   if source == str(ipaddress.ip_network(s).network_address + 1)), None)
    if subnet is None:
      continue
    for target in targets:
      if (not wlan_address(target) or ipaddress.ip_address(target) not in subnet
          or target in (source, str(subnet.network_address), str(subnet.broadcast_address))):
        continue
      try:
        route = query(target, source)
        if (route and route[0].get('dev') == interface and not route[0].get('gateway')
            and route[0].get('type', 'unicast') == 'unicast'):
          return interface, source, target
      except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        continue
  return None


def receiver_network(network, packet=None, query=None):
  """Bind only the imported direct AP's active station, ignoring phone changes.

  Use the physical-USB provisioning identity, not an SSID guess or an arbitrary
  private IP. The immutable network importer already derives the same UUID.
  No new root service or protected-base write is required.
  """
  import uuid
  packet = read_packet() if packet is None else packet
  if packet is None:
    return ()
  value = packet[1]
  if value.get('wifi_policy') != 'direct_only' or len(value.get('profiles', [])) != 1:
    return ()
  subnet = value.get('direct_subnet')
  if subnet not in DIRECT_SUBNETS:
    return ()
  uid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'carrot-usb:' + value['owner'] + ':' + value['profiles'][0]['id']))
  if query is None:
    def query():
      return subprocess.check_output(['nmcli', '--escape', 'no', '-t', '-f', 'UUID,DEVICE',
                                      'connection', 'show', '--active'], timeout=1., text=True)
  active = {row.partition(':')[2] for row in query().splitlines() if row.partition(':')[0] == uid}
  subnet = ipaddress.ip_network(subnet)
  return tuple((interface, address) for interface, address in network
               if interface.startswith('wl') and interface in active
               and ipaddress.ip_address(address) in subnet
               and address not in (str(subnet.network_address), str(subnet.network_address + 1), str(subnet.broadcast_address)))


def direct_peer(peer, local):
  """The only comma peer on a direct AP subnet is its .1 gateway."""
  return any(ipaddress.ip_address(local) in ipaddress.ip_network(subnet)
             and peer == str(ipaddress.ip_network(subnet).network_address + 1) for subnet in DIRECT_SUBNETS)
