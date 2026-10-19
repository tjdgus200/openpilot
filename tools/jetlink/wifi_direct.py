"""Branch-owned comma AP, independent of the optional phone Internet uplink.

This low-priority worker never reconnects wlan0 or starts an AP onroad.
The owner requests power saving disabled on both connected Wi-Fi interfaces.
Its secret stays in a root-only file and the existing private USB provisioning.
Returning to a branch without this helper tears down only its own AP.
"""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import struct
import subprocess
import threading
import time

from persistent_state import durable_write
from wifi_direct_netlink import INTERFACE, Wireless, profile_id
from hud_wifi_route import DIRECT_SUBNETS as SUBNETS
from wifi_power import configure as configure_power

STATUS = Path('/dev/shm/carrot-jetlink-direct-wifi.json')
PRIVATE = Path('/data/carrot-jetlink-direct-wifi')
PROFILE = Path('/run/NetworkManager/system-connections/carrot-jetson-direct.nmconnection')
ACTIVE_SOURCE = Path('/data/openpilot/tools/jetlink/wifi_direct.py')
OBSERVATION_ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError,
                      struct.error, StopIteration, subprocess.SubprocessError)


class ChannelUnavailable(ValueError):
  """A stable, secret-free channel diagnostic; never include nmcli output."""


def source_action(source, digest, parked):
  """Preserve the AP across compatible updates; retire it on official return."""
  try:
    policy = source.with_name('wifi_direct_policy.json')
    if policy.is_symlink() or policy.stat().st_size > 512:
      return 'stop'
    if json.loads(policy.read_text()) != {'format': 1, 'direct_only': True}:
      return 'stop'
    if hashlib.sha256(source.read_bytes()).digest() == digest:
      return 'continue'
    return 'handover' if parked else 'defer'
  except (OSError, ValueError):
    return 'stop'


def offroad():
  try:
    p = Path('/data/params/d')
    return (p/'IsOffroad').read_bytes() == b'1' and (p/'IsOnroad').read_bytes() == b'0'
  except OSError:
    return False


def nm(*args):
  value = subprocess.run(['nmcli', '--escape', 'no', *args], capture_output=True, text=True, timeout=15,
                         env=dict(os.environ, LC_ALL='C'))
  if value.returncode:
    raise RuntimeError('Direct Wi-Fi operation failed')  # Never include NM output/secrets.
  return value.stdout.strip()


def choose_subnet(routes):
  occupied = [ipaddress.ip_network(r['dst'], strict=False) for r in routes
              if r.get('dst', 'default') != 'default' and r.get('dev') != INTERFACE]
  return next(s for s in SUBNETS if not any(ipaddress.ip_network(s).overlaps(r) for r in occupied))


def station_channel():
  # Query the existing scan cache only; never trigger a scan or move wlan0.
  rows = nm('-t', '-f', 'IN-USE,FREQ', 'device', 'wifi', 'list', 'ifname', 'wlan0', '--rescan', 'no').splitlines()
  current = next((r.partition(':')[2].strip() for r in rows if r.startswith('*:')), None)
  if current is None:
    return None
  # nmcli emits "5745 MHz" on AGNOS, not just "5745". A parsing failure
  # must never silently put a shared-radio AP on a different default channel.
  match = re.fullmatch(r'(\d+)(?:\s+MHz)?', current, re.IGNORECASE)
  if match is None:
    raise ChannelUnavailable('station_frequency_unavailable')
  freq = int(match[1])
  if freq in range(2412, 2473, 5):
    return 'bg', (freq - 2407) // 5
  if (freq - 5000) % 5:
    raise ChannelUnavailable('station_channel_unsupported')
  number = (freq - 5000) // 5
  if number not in (36, 40, 44, 48, 149, 153, 157, 161):
    raise ChannelUnavailable('station_channel_unsupported')
  return 'a', number


def channel():
  return station_channel() or ('a', 36)


def configuration():
  PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
  if PRIVATE.is_symlink():
    raise ValueError('Unexpected direct Wi-Fi directory')
  os.chmod(PRIVATE, 0o700)
  path = PRIVATE/'identity.json'
  if path.is_symlink():
    raise ValueError('Unexpected direct Wi-Fi identity')
  if path.exists():
    value = json.loads(path.read_text())
    if (value.get('uuid') != profile_id() or value.get('subnet') not in SUBNETS
        or not isinstance(value.get('ssid'), str) or not re.fullmatch('Carrot-Jetson-[0-9a-f]{8}', value['ssid'])
        or not isinstance(value.get('password'), str)
        or len(value['password']) != 32 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in value['password'])):
      raise ValueError('Invalid direct Wi-Fi identity')
    return value
  routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show'], text=True, timeout=3))
  value = {'uuid': profile_id(), 'subnet': choose_subnet(routes),
           'ssid': 'Carrot-Jetson-' + hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()[:8],
           'password': secrets.token_urlsafe(24)}
  durable_write(path, json.dumps(value).encode(), 0o600)
  return value


def render(value, band, number):
  gateway = str(ipaddress.ip_network(value['subnet']).network_address + 1)
  return ('[connection]\nid=Carrot Jetson direct\nuuid=' + value['uuid'] + '\ntype=wifi\ninterface-name=' + INTERFACE + '\nautoconnect=false\n'
          '[wifi]\nmode=ap\npowersave=2\nssid=' + value['ssid'] + '\nband=' + band + '\nchannel=' + str(number) + '\n'
          '[wifi-security]\nkey-mgmt=wpa-psk\nproto=rsn\npairwise=ccmp\ngroup=ccmp\npsk=' + value['password'] + '\n'
          '[ipv4]\nmethod=shared\naddress1=' + gateway + '/24\nnever-default=true\n'
          '[ipv6]\nmethod=disabled\n')


class AccessPoint:
  def __init__(self, wireless, command=nm):
    self.wireless, self.command = wireless, command
    self.index = self.value = None
    self.previous_power_save = None
    self.band_channel = None
    self.next_band_channel = None
    self.stage = 'idle'

  def adopt(self):
    """An offroad code handover adopts only this identity's existing AP."""
    rows = [r for r in self.wireless.interfaces() if r['name'] == INTERFACE]
    if not rows:
      return False
    value = configuration()
    if (self.command('-g', 'connection.interface-name', 'connection', 'show', 'uuid', value['uuid']) != INTERFACE
        or self.command('-g', '802-11-wireless.mode', 'connection', 'show', 'uuid', value['uuid']) != 'ap'
        or rows[0].get('type') not in (2, 3)):
      raise ValueError('Reserved AP cannot be adopted')
    band = self.command('-g', '802-11-wireless.band', 'connection', 'show', 'uuid', value['uuid'])
    number = int(self.command('-g', '802-11-wireless.channel', 'connection', 'show', 'uuid', value['uuid']))
    self.index, self.value = rows[0]['index'], value
    self.band_channel, self.stage = (band, number), 'active'
    return True

  def start(self, band_channel=None):
    if not offroad():
      return False
    self.stage = 'identity'
    value = configuration()
    routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show'], text=True, timeout=3))
    if any(r.get('dev') != INTERFACE and r.get('dst', 'default') != 'default'
           and ipaddress.ip_network(value['subnet']).overlaps(ipaddress.ip_network(r['dst'], strict=False)) for r in routes):
      raise ValueError('Direct Wi-Fi subnet conflict')
    self.stage = 'channel'
    band, number = band_channel or channel()
    if not offroad():
      return False
    self.stage = 'interface'
    self.index = self.wireless.create()
    self.value = value
    self.stage = 'register_device'
    until = time.monotonic() + 3.
    while True:
      try:
        if self.command('-g', 'GENERAL.STATE', 'device', 'show', INTERFACE):
          break
      except RuntimeError:
        pass
      if time.monotonic() >= until:
        raise RuntimeError('Direct Wi-Fi device registration timeout')
      time.sleep(.2)
    self.command('device', 'set', INTERFACE, 'autoconnect', 'no', 'managed', 'yes')
    self.stage = 'profile'
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    durable_write(PROFILE, render(value, band, number).encode(), 0o600)
    self.command('connection', 'load', str(PROFILE))
    if not offroad():
      self.stage = 'waiting_offroad'
      return False
    self.stage = 'activate'
    self.command('--wait', '12', 'connection', 'up', 'uuid', value['uuid'], 'ifname', INTERFACE)
    self.band_channel = (band, number)
    self.stage = 'power_save'
    # The connection is already up. A power-policy query failure must not turn
    # successful activation into destructive activation cleanup.
    self.stage = 'active'
    return True

  def awake(self):
    # Preserve AP ownership checks. The shared-radio Internet station must also
    # stay awake; this changes no channel, network selection, or connection.
    if not any(r['name'] == INTERFACE and r['index'] == self.index for r in self.wireless.interfaces()):
      raise ValueError('Direct AP ownership lost')
    if self.index is not None and self.wireless.power_save(self.index) != 0:
      self.wireless.set_power_save(self.index, 0)
    for interface in self.wireless.interfaces():
      if interface['name'] == 'wlan0' and interface.get('type') == 2 and self.wireless.power_save(interface['index']) != 0:
        self.wireless.set_power_save(interface['index'], 0)

  def active(self):
    return self.value is not None and self.value['uuid'] in self.command('-t', '-f', 'UUID', 'connection', 'show', '--active').splitlines()

  def needs_rechannel(self):
    desired = station_channel()
    self.next_band_channel = desired
    return desired is not None and desired != self.band_channel

  def close(self):
    previous, self.previous_power_save = self.previous_power_save, None
    if self.index is not None and previous is not None:
      try:
        if any(r['name'] == INTERFACE and r['index'] == self.index for r in self.wireless.interfaces()):
          self.wireless.set_power_save(self.index, previous)
      except OSError:
        pass  # Still tear down our AP if the driver disappeared during close.
    try:
      if self.value is not None:
        self.command('--wait', '3', 'connection', 'down', 'uuid', self.value['uuid'])
    except (RuntimeError, subprocess.SubprocessError):
      pass
    finally:
      if self.index is not None:
        self.wireless.delete(self.index)
        self.index = None
      if self.value is not None:
        try:
          self.command('connection', 'delete', 'uuid', self.value['uuid'])
        except (RuntimeError, subprocess.SubprocessError):
          pass
      self.value = None
      self.band_channel = None


class Supervisor:
  def __init__(self, ap):
    self.ap = ap
    self.state, self.reason = 'starting', ''

  def step(self):
    ap = self.ap
    self.reason = ''
    try:
      active = ap.active()
    except OBSERVATION_ERRORS:
      self.state, self.reason = 'degraded', 'ap_query_failed'
      return 5.
    if not active:
      if not offroad():
        self.state, self.reason = 'waiting_offroad', 'ap_inactive'
        return 5.
      try:
        ap.close()  # A confirmed inactive AP, offroad only.
        self.state = 'active' if ap.start() else 'waiting_offroad'
      except OBSERVATION_ERRORS as error:
        self.reason = str(error) if isinstance(error, ChannelUnavailable) else 'ap_start_failed'
        try:
          ap.close()  # Only a partial/failed activation is destructive.
        except OBSERVATION_ERRORS:
          self.reason = 'ap_cleanup_failed'
        self.state = 'unavailable'
        return 30.
    else:
      self.state = 'active'
      if offroad():
        try:
          realign = ap.needs_rechannel()
        except OBSERVATION_ERRORS as error:
          realign = False
          self.reason = str(error) if isinstance(error, ChannelUnavailable) else 'channel_query_failed'
        if realign and offroad():
          # A validated channel change is a deliberate offroad operation.
          try:
            desired = ap.next_band_channel
            ap.close()
            self.state = 'active' if ap.start(desired) else 'waiting_offroad'
          except OBSERVATION_ERRORS:
            self.state, self.reason = 'unavailable', 'channel_realign_failed'
            return 30.
    if self.state == 'active':
      try:
        ap.awake()
      except OBSERVATION_ERRORS:
        self.reason = self.reason or 'power_policy_retry'
        # Keep a known-active AP, its DHCP state and existing TCP connections.
    return 5.


def ensure():
  """Nonblocking: the existing USB owner never waits for AP startup."""
  try:
    if time.monotonic() - json.loads(STATUS.read_text())['updated'] < 10:
      return
  except (OSError, ValueError, KeyError, TypeError):
    pass
  subprocess.Popen(['sudo', '-n', '/usr/bin/python3', str(Path(__file__).resolve()), '--serve'],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def low_priority():
  os.setpriority(os.PRIO_PROCESS, 0, 19)
  os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  # A USB worker can inherit a big-core-only affinity. Move this independent
  # root control worker to little CPUs rather than intersecting that mask.
  os.sched_setaffinity(0, set(range(min(4, os.cpu_count() or 1))))


def serve(trial=0):
  if os.geteuid() != 0:
    raise PermissionError('Direct Wi-Fi requires its root control worker')
  lock = open('/run/carrot-jetlink-direct-wifi.lock', 'a')
  try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    lock.close()
    return
  low_priority()
  stop = threading.Event()
  for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: stop.set())
  digest = hashlib.sha256(Path(__file__).read_bytes()).digest()
  wireless, ap = None, None
  preserve = False
  power_defaults, next_power = False, 0.
  expires, retry, state = time.monotonic() + trial if trial else float('inf'), 0., 'starting'
  try:
    wireless = Wireless()
    ap = AccessPoint(wireless)
    supervisor = Supervisor(ap)
    adopted = False
    while not stop.is_set() and time.monotonic() < expires:
      action = source_action(ACTIVE_SOURCE, digest, offroad()) if not trial else 'continue'
      if action in ('stop', 'handover'):
        preserve = action == 'handover'
        break
      now = time.monotonic()
      if now >= retry:
        try:
          if not adopted:
            ap.adopt()
            adopted = True
          retry = now + supervisor.step()
        except OBSERVATION_ERRORS:
          # An ownership/NM observation failure is not authority to delete a vdev.
          supervisor.state, supervisor.reason = 'degraded', 'ap_adoption_retry'
          retry = now + 5.
        state = supervisor.state
      # No SSID, password, peer identity or vehicle payload in diagnostics.
      durable_write(STATUS, json.dumps({'state': state, 'stage': ap.stage, 'interface': INTERFACE,
                                      'reason': supervisor.reason, 'index': ap.index,
                                      'reload_pending': action == 'defer',
                                      'updated': now}).encode(), 0o644)
      if state == 'active' and not power_defaults and now >= next_power:
        # Establish the AP first; optional edits of unrelated saved profiles
        # must not consume the initial offroad activation window.
        try:
          configure_power()
          power_defaults = True
        except OBSERVATION_ERRORS:
          next_power = now + 60.
      stop.wait(2.)
  finally:
    if ap is not None and not preserve:
      ap.close()
    if wireless is not None:
      wireless.close()
    STATUS.unlink(missing_ok=True)
    lock.close()


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--serve', action='store_true')
  parser.add_argument('--trial', type=int, choices=range(1, 181), default=0)
  args = parser.parse_args()
  if not args.serve and not args.trial:
    parser.error('Expected --serve or a bounded --trial')
  serve(args.trial)
