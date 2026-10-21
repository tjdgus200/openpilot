"""Read NetworkManager secrets only in a low-priority, private USB worker."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from wifi_protocol import HEADER, LIMIT, validate
from wifi_direct_netlink import INTERFACE as DIRECT_INTERFACE, profile_id as direct_profile_id
from openpilot.common.jetson_model import selected_release


def nm(*args):
  result = subprocess.run(['nmcli', '--escape', 'no', *args], capture_output=True,
                          text=True, timeout=2, env=dict(os.environ, LC_ALL='C'))
  if result.returncode:
    raise RuntimeError('NetworkManager query unavailable')
  return result.stdout.rstrip('\n')


def direct_client_id(uid):
  # Legacy protected Jetson workers do not reconsider their preferred profile
  # after roaming to a fallback. A new AP instance is a new client connection
  # identity, so their existing transactional importer retries it exactly once.
  # Phone association changes leave both inputs unchanged; no periodic churn.
  boot = str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
  index = int((Path('/sys/class/net') / DIRECT_INTERFACE / 'ifindex').read_text())
  return str(uuid.uuid5(uuid.UUID(uid), f'client:{boot}:{index}'))


def collect():
  # Never read/export phone or other saved client credentials. Uplink changes
  # must not alter the provisioning identity and reconnect the Jetson station.
  uid = direct_profile_id()
  active = nm('-t', '-f', 'UUID', 'connection', 'show', '--active').splitlines()
  if uid not in active:
    return []
  if nm('-g', 'connection.interface-name', 'connection', 'show', 'uuid', uid) != DIRECT_INTERFACE:
    return []
  fields = nm('--show-secrets', '-g',
    '802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.hidden,802-11-wireless-security.key-mgmt,802-11-wireless-security.psk',
    'connection', 'show', 'uuid', uid).split('\n')
  fields += [''] * (5 - len(fields))
  if len(fields) != 5:
    raise ValueError('Invalid direct AP settings')
  ssid, mode, hidden, security, password = fields
  if mode != 'ap' or security != 'wpa-psk' or not password:
    raise ValueError('Owned direct AP unavailable')
  profile = {'id': direct_client_id(uid), 'ssid': ssid, 'hidden': hidden in ('yes', 'true'),
             'active': True, 'security': security, 'password': password}
  validate({'version': 1, 'owner': '0' * 64, 'profiles': [profile]})
  return [profile]


def provisioning(owner, onroad):
  profiles = collect()
  # Derive the observed AP prefix without opening its root-only identity file.
  # Only our active AP may supply it. Empty means wait; never substitute wlan0.
  subnet = None
  if profiles:
    address = nm('-g', 'ipv4.addresses', 'connection', 'show', 'uuid', direct_profile_id())
    interface = ipaddress.ip_interface(address)
    if interface.ip != interface.network.network_address + 1:
      raise ValueError('Invalid direct AP address')
    subnet = str(interface.network)
  return validate({'version': 1, 'owner': owner, 'profiles': profiles, 'onroad': onroad,
                   'wifi_policy': 'direct_only', 'direct_subnet': subnet})


def road_state(raw):
  # Current typed Params returns bool; older installations returned bytes.
  if type(raw) is bool:
    return raw
  if raw in (b'0', '0'):
    return False
  if raw in (b'1', '1'):
    return True
  return None


def main(fd):
  from openpilot.common.params import Params
  params = Params()
  owner = hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
  release = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/modeld/jetlink/host_release.json'
  manifest = selected_release(release)
  channel = socket.socket(fileno=fd)
  channel.setblocking(False)
  config = None
  refresh = 0
  next_ap = 0
  while True:
    now = time.monotonic()
    if now >= next_ap:
      try:
        from wifi_direct import ensure
        ensure()
      except Exception:
        pass  # Failure of this display side channel must not block USB.
      next_ap = now + 30
    if now >= refresh:
      try:
        config = provisioning(owner, None)
        refresh = now + 30
      except Exception:
        # Keep boot-update control alive even without a usable AP/NM query.
        # Legacy recovery treats an empty profile set as non-destructive.
        config = {'version': 1, 'owner': owner, 'profiles': [], 'onroad': None,
                  'wifi_policy': 'direct_only', 'direct_subnet': None}
        refresh = now + 5
    if config is not None:
      raw = params.get('IsOnroad')
      onroad = road_state(raw)
      value = dict(config, onroad=onroad)
      if manifest is not None:
        value['jetson_release'] = manifest
      data = json.dumps(value, separators=(',', ':')).encode()
      if len(data) <= LIMIT:
        try:
          channel.send(HEADER.pack(time.monotonic()) + data)
        except BlockingIOError:
          pass
    time.sleep(2)


if __name__ == '__main__':
  main(int(sys.argv[1]))
