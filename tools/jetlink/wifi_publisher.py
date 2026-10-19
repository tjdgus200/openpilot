"""Read NetworkManager secrets only in a low-priority, private USB worker."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

from wifi_protocol import HEADER, LIMIT, validate
from wifi_direct_netlink import INTERFACE as DIRECT_INTERFACE, profile_id as direct_profile_id
from openpilot.common.jetson_model import selected_release


def ap_generation(now):
  # Cheap tmpfs hint, not a NetworkManager scan or a credential read.
  try:
    value = json.loads(Path('/dev/shm/carrot-jetlink-direct-wifi.json').read_text())
    if (0 <= now - value['updated'] < 10 and value.get('state') in ('active', 'degraded')
        and type(value.get('index')) is int):
      return value['index']
  except (OSError, ValueError, KeyError, TypeError):
    pass
  return None


def ap_diagnostic(now):
  try:
    value = json.loads(Path('/dev/shm/carrot-jetlink-direct-wifi.json').read_text())
    if not 0 <= now - value['updated'] < 10:
      return ''
    reason = value.get('reason')
    if reason in ('station_channel_unsupported', 'station_frequency_unavailable'):
      return 'comma_ap_channel_unavailable'
    if value.get('state') == 'waiting_offroad':
      return 'comma_ap_waiting_offroad'
    if value.get('state') in ('unavailable', 'degraded'):
      return 'comma_ap_unavailable'
  except (OSError, ValueError, KeyError, TypeError):
    pass
  return ''


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
  # Read only the owned AP's secret. Phone/router profiles stay on comma and
  # never become Jetson peer-data fallbacks, even while the AP is unavailable.
  active = nm('-t', '-f', 'UUID', 'connection', 'show', '--active').splitlines()
  try:
    uid = direct_profile_id()
  except OSError:
    return []
  if uid not in active:
    return []
  if nm('-g', 'connection.interface-name', 'connection', 'show', 'uuid', uid) != DIRECT_INTERFACE:
    return []
  fields = nm('--show-secrets', '-g',
    '802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.hidden,802-11-wireless-security.key-mgmt,802-11-wireless-security.psk',
    'connection', 'show', 'uuid', uid).split('\n')
  fields += [''] * (5 - len(fields))
  if len(fields) != 5:
    return []
  ssid, mode, hidden, security, password = fields
  if mode != 'ap' or security not in ('wpa-psk', 'sae'):
    return []
  if not password:
    raise RuntimeError('Saved direct Wi-Fi secret unavailable')
  profile = dict(id=direct_client_id(uid), ssid=ssid, hidden=hidden in ('yes', 'true'),
                 active=True, security=security, password=password)
  validate(dict(version=1, owner='0' * 64, profiles=[profile]))
  return [profile]


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
  profiles = None
  refresh = 0
  next_ap = 0
  generation = None
  while True:
    now = time.monotonic()
    if now >= next_ap:
      try:
        from wifi_direct import ensure
        ensure()
      except Exception:
        pass  # Failure of this display side channel must not block USB.
      next_ap = now + 5
    current = ap_generation(now)
    if now >= refresh or current != generation:
      try:
        profiles = collect()
        refresh = now + (30 if profiles else 2)
        generation = current
      except Exception:
        # Do not send an empty deletion set after an access/query failure.
        profiles = None
        refresh = now + 5
    if profiles is not None:
      raw = params.get('IsOnroad')
      onroad = road_state(raw)
      value = dict(version=1, owner=owner, profiles=profiles, onroad=onroad)
      value['direct_ap_reason'] = ap_diagnostic(now)
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
