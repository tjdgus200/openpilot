"""Opt-in, single-device BLE battery reader; never used by vehicle controls.

The HKMC 0101/0105/0106 layouts are candidates for PV5. Only individually compared fields
from the local configuration reach the HUD. No general-purpose ELM/UDS API.
"""
import asyncio
from array import array
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

from openpilot.selfdrive.carrot.cluster.cluster_battery import finite_number, packet_from_metrics, valid_value

CONFIG = Path('/data/jetlink/obd-battery.json')
SNAPSHOT = Path('/dev/shm/carrot-obd-battery.json')
SERVICE = 'e7810a71-73ae-499d-8c15-faa9aef0c3f2'
UART = 'bef8d6c9-9c21-4c9e-b632-bd58c1009f9f'
DEVICE = 'org.bluez.Device1'
GATT = 'org.bluez.GattCharacteristic1'
ADAPTER = 'org.bluez.Adapter1'
PERIOD = 2.0
RETRY_INTERVAL = 5.0
RETRY_WINDOW = 60.0
RECOVERY_WINDOW = 300.0
FIELDS = frozenset(('max_temp_c', 'min_temp_c', 'pack_power_kw', 'cell_delta_mv', 'coolant_inlet_c',
                    'soc_percent', 'thermal_mode', 'temp_change_5min_c'))
REPLIES = {'220101': (62, b'\x62\x01\x01'), '220105': (46, b'\x62\x01\x05'), '220106': (38, b'\x62\x01\x06')}
SETUP = ('ATE0', 'ATL0', 'ATM0', 'ATSP6', 'ATCAF1', 'ATCFC1', 'ATFCSM0',
         'ATSH7E4', 'ATCRA7EC', 'ATH1', 'ATD0', 'ATS0', 'ATAL', 'ATST96')
COMMANDS = frozenset((*SETUP, 'ATI', *REPLIES))


def read_json(path, limit=4096):
  with path.open('rb') as stream:
    raw = stream.read(limit + 1)
  if len(raw) > limit:
    raise ValueError('oversize configuration')
  return json.loads(raw)


def configuration(value):
  if not isinstance(value, dict) or set(value) != {'version', 'address', 'confirmed_fields'}:
    raise ValueError('configuration schema')
  if type(value['version']) is not int or value['version'] != 1:
    raise ValueError('configuration version')
  address = value['address']
  if not isinstance(address, str) or not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address):
    raise ValueError('device address')
  fields = value['confirmed_fields']
  if not isinstance(fields, list) or len(fields) > len(FIELDS) or any(not isinstance(f, str) or f not in FIELDS for f in fields):
    raise ValueError('unverified metric')
  if len(set(fields)) != len(fields):
    raise ValueError('duplicate metric')
  if 'temp_change_5min_c' in fields and 'max_temp_c' not in fields:
    raise ValueError('trend requires calibrated maximum temperature')
  return address, frozenset(fields)


def config_id(config):
  address, fields = config
  return hashlib.sha256(json.dumps([address, sorted(fields)]).encode()).hexdigest()


def parse_reply(reply, command='220101'):
  """One prompt-terminated ELM transaction, one 7EC ISO-TP payload only."""
  if command not in REPLIES:
    raise ValueError('unsupported DID')
  expected_length, prefix = REPLIES[command]
  if not isinstance(reply, bytes) or len(reply) > 4096 or reply.count(b'>') != 1 or not reply.endswith(b'>'):
    raise ValueError('ELM framing')
  lines = reply[:-1].decode('ascii').replace('\n', '\r').split('\r')
  payload, length, sequence = bytearray(), None, 1
  for line in lines:
    line = line.strip().replace(' ', '').upper()
    if not line:
      continue
    if not re.fullmatch(r'7EC[0-9A-F]{16}', line):
      raise ValueError('ELM response source/status')
    frame = bytes.fromhex(line[3:])
    if length is None:
      if frame[:2] != bytes((0x10, expected_length)):
        raise ValueError('unexpected DID length')
      length = expected_length
      payload.extend(frame[2:])
    else:
      if len(payload) >= length or frame[0] != 0x20 | sequence:
        raise ValueError('ISO-TP sequence')
      payload.extend(frame[1:1 + min(7, length - len(payload))])
      sequence = (sequence + 1) & 15
  if len(payload) != expected_length or payload[:3] != prefix:
    raise ValueError('incomplete or wrong DID')
  return bytes(payload)


def candidate_metrics(payload):
  """0101 data offsets exclude 62 01 01. Never substitute coolant/SOC meanings.

  Reference: Esprit1st/Hyundai-Ioniq-5-Torque-Pro-PIDs, 0101 K/L, M/N,
  O/P, X/Z. Other models' maps are not PV5 calibration evidence.
  """
  if len(payload) != 62 or payload[:3] != b'\x62\x01\x01':
    raise ValueError('payload layout')
  data = payload[3:]
  current = int.from_bytes(data[10:12], 'big', signed=True) / 10
  voltage = int.from_bytes(data[12:14], 'big') / 10
  maximum = int.from_bytes(data[14:15], 'big', signed=True)
  minimum = int.from_bytes(data[15:16], 'big', signed=True)
  high, low = data[23] / 50, data[25] / 50
  result = {}
  if -40 <= minimum <= maximum <= 90:
    result.update(max_temp_c=maximum, min_temp_c=minimum)
  if 150 <= voltage <= 550 and -1000 <= current <= 1000:
    result['pack_power_kw'] = voltage * current / 1000
  if 2.0 <= low <= high <= 4.5:
    result['cell_delta_mv'] = round((high - low) * 1000, 3)
  return {name: value for name, value in result.items() if valid_value(name, value)}


def coolant_candidate(payload):
  """eGMP 0106 candidate, NOT an independently calibrated PV5 mapping.

  nickn17/evDash CarHyundaiEgmp.cpp uses response hex characters 14:16:
  payload byte 7, or data byte 4 after 62 01 06, signed Celsius.
  Accept only the 38-byte layout observed in the supplied scanner log.
  Do not use older Kona/Niro 0101 D22 as a substitute.
  """
  if len(payload) != 38 or payload[:3] != b'\x62\x01\x06':
    raise ValueError('coolant payload layout')
  value = int.from_bytes(payload[7:8], 'big', signed=True)
  values = {'coolant_inlet_c': value} if -30 <= value <= 120 else {}
  # Same eGMP reference, data byte 9. Unknown/low-temperature-range codes
  # do not establish heating or cooling and must not appear as idle.
  mode = {0: 'idle', 185: 'cooling', 125: 'heating'}.get(payload[12])
  if mode is not None:
    values['thermal_mode'] = mode
  return values


def soc_candidate(payload):
  """Ioniq 5 0105 AF/2: display SOC candidate, not 0101 BMS/internal SOC."""
  if len(payload) != 46 or payload[:3] != b'\x62\x01\x05':
    raise ValueError('SOC payload layout')
  value = payload[34] / 2
  return {'soc_percent': value} if value <= 100 else {}


class TemperatureTrend:
  """Fixed numeric ring; no CAN payloads, per-sample objects or growing list."""
  CAPACITY = 153

  def __init__(self):
    self.times = array('d', [0.0]) * self.CAPACITY
    self.values = array('d', [0.0]) * self.CAPACITY
    self.head = self.count = 0

  def update(self, stamp, value):
    if not finite_number(stamp) or not valid_value('max_temp_c', value):
      self.head = self.count = 0
      return None
    if self.count:
      last = self.times[(self.head + self.count - 1) % self.CAPACITY]
      if not 0 < stamp - last <= 5:
        self.head = self.count = 0
      elif stamp - last < PERIOD:
        return None
    while self.count and stamp - self.times[self.head] > 302:
      self.head = (self.head + 1) % self.CAPACITY
      self.count -= 1
    if self.count == self.CAPACITY:
      self.head = (self.head + 1) % self.CAPACITY
      self.count -= 1
    index = (self.head + self.count) % self.CAPACITY
    self.times[index], self.values[index] = stamp, value
    self.count += 1
    if 298 <= stamp - self.times[self.head] <= 302:
      return value - self.values[self.head]
    return None


async def poll_metrics(link):
  # Timestamp before all requests: later reads cannot rejuvenate older
  # 0101 values. Any timeout/malformed response aborts the complete cycle.
  observed_at = time.monotonic()
  values = candidate_metrics(parse_reply(await link.command('220101')))
  values.update(soc_candidate(parse_reply(await link.command('220105'), '220105')))
  values.update(coolant_candidate(parse_reply(await link.command('220106'), '220106')))
  return observed_at, values


def write_snapshot(state, candidates=None, confirmed=frozenset(), path=SNAPSHOT, identity=None, observed_at=None):
  candidates = candidates or {}
  record = {'version': 1, 'updated': time.monotonic() if observed_at is None else observed_at, 'state': state, 'config_id': identity,
            'candidates': candidates, 'metrics': {k: v for k, v in candidates.items() if k in confirmed}}
  raw = json.dumps(record, allow_nan=False, separators=(',', ':')).encode()
  if len(raw) > 4096:
    raise ValueError('snapshot size')
  temporary = path.with_suffix('.tmp')
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, 'wb') as stream:
    stream.write(raw)
  os.replace(temporary, path)


def snapshot_packet(record, now, expected_identity=None):
  """HUD reads confirmed fields only; PC/Jetson clocks are not mixed here."""
  if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
    return packet_from_metrics({}, now)
  if expected_identity is None or record.get('config_id') != expected_identity:
    return packet_from_metrics({}, now)
  stamp = record.get('updated')
  if not finite_number(stamp) or not 0 <= now - stamp <= 5 or record.get('state') != 'receiving':
    return packet_from_metrics({}, now)
  values = record.get('metrics')
  if not isinstance(values, dict) or len(values) > len(FIELDS):
    return packet_from_metrics({}, now)
  metrics = {k: (v, stamp) for k, v in values.items() if k in FIELDS and valid_value(k, v)}
  if metrics.get('min_temp_c', (-50,))[0] > metrics.get('max_temp_c', (130,))[0]:
    metrics.pop('min_temp_c', None)
    metrics.pop('max_temp_c', None)
  return packet_from_metrics(metrics, now, 'receiving' if metrics else 'profile_required')


class NotificationQueue(asyncio.Queue):
  """Jeepney silently drops full queues; make that a failed transaction."""
  def __init__(self):
    super().__init__(maxsize=32)
    self.overflow = False

  def put_nowait(self, item):
    # StartNotify/StopNotify also emit PropertiesChanged. Only UART bytes
    # belong to the bounded transaction queue, not Notifying/status changes.
    if len(item.body) != 3 or item.body[0] != GATT or 'Value' not in item.body[1]:
      return
    if len(item.body[1]['Value'][1]) > 512:
      self.overflow = True
      return
    if self.full():
      self.overflow = True
    super().put_nowait(item)


def props(values):
  return {key: value[1] for key, value in values.items()}


class Link:
  def __init__(self, router, address):
    self.router, self.address = router, address
    self.device = self.uart = self.scan = None
    self.queue = NotificationQueue()
    self.filter = None
    self.notifying = False

  async def call(self, path, interface, method, signature=None, body=(), bus='org.bluez', timeout=5):  # noqa: ASYNC109
    from jeepney import DBusAddress, new_method_call
    from jeepney.wrappers import unwrap_msg
    message = new_method_call(DBusAddress(path, bus_name=bus, interface=interface), method, signature, body)
    return unwrap_msg(await asyncio.wait_for(self.router.send_and_get_reply(message), timeout))

  async def objects(self):
    return (await self.call('/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'))[0]

  async def connect(self):
    from jeepney import MatchRule
    objects = await self.objects()
    # Discovery is bounded and scoped to this D-Bus connection. Never pair/trust
    # or connect any discovered address other than the local allowlisted target.
    for path, interfaces in objects.items():
      if ADAPTER in interfaces and props(interfaces[ADAPTER]).get('Powered'):
        await self.call(path, ADAPTER, 'StartDiscovery')
        self.scan = path
        break
    for _ in range(10):
      objects = await self.objects()
      matches = [p for p, i in objects.items() if DEVICE in i and props(i[DEVICE]).get('Address', '').upper() == self.address]
      if matches:
        self.device = sorted(matches)[0]
        break
      await asyncio.sleep(1)
    if self.device is None:
      raise ValueError('device unavailable')
    await self.call(self.device, DEVICE, 'Connect', timeout=15)
    for _ in range(10):
      objects = await self.objects()
      device = props(objects.get(self.device, {}).get(DEVICE, {}))
      if device.get('Connected') and device.get('ServicesResolved'):
        break
      await asyncio.sleep(1)
    else:
      raise ValueError('services unavailable')
    if self.scan:
      await self.call(self.scan, ADAPTER, 'StopDiscovery')
      self.scan = None
    services = {p for p, i in objects.items() if p.startswith(self.device + '/') and
                props(i.get('org.bluez.GattService1', {})).get('UUID') == SERVICE}
    chars = [(p, props(i[GATT])) for p, i in objects.items() if p.startswith(self.device + '/') and GATT in i]
    matches = [p for p, v in chars if v.get('UUID') == UART and v.get('Service') in services
               and {'write', 'notify'} <= set(v.get('Flags', []))]
    if len(matches) != 1:
      raise ValueError('unsupported GATT')
    # Device information checks reduce accidental wrong-device selection; a BLE
    # address or these strings are not cryptographic authentication.
    for uuid, expected in (('2a24', b'JXSL-MIC34LE'), ('2a29', b'JINXUSOLU')):
      paths = [p for p, v in chars if v.get('UUID') == f'0000{uuid}-0000-1000-8000-00805f9b34fb']
      if len(paths) != 1 or bytes((await self.call(paths[0], GATT, 'ReadValue', 'a{sv}', ({},)))[0]).rstrip(b'\0') != expected:
        raise ValueError('device identity')
    self.uart = matches[0]
    owner = (await self.call('/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetNameOwner', 's', ('org.bluez',),
                             bus='org.freedesktop.DBus'))[0]
    rule = MatchRule(type='signal', sender=owner, path=self.uart,
                     interface='org.freedesktop.DBus.Properties', member='PropertiesChanged')
    await self.call('/org/freedesktop/DBus', 'org.freedesktop.DBus', 'AddMatch', 's', (rule.serialise(),),
                    bus='org.freedesktop.DBus')
    self.filter = self.router.filter(rule, queue=self.queue)
    self.filter.__enter__()
    await self.call(self.uart, GATT, 'StartNotify')
    self.notifying = True
    # Require a distinct adapter identity reply before configuring a new
    # session. No poll follows a timeout: rebuild the entire connection.
    reply = await self.command('ATI')
    if not re.fullmatch(rb'(?:ATI\r\n?)?\s*ELM327 v[0-9.]+\s*>', reply):
      raise ValueError('ELM identity')
    for command in SETUP:
      reply = await self.command(command)
      lines = [s.strip() for s in reply[:-1].decode('ascii').replace('\n', '\r').split('\r') if s.strip()]
      if lines not in ([command, 'OK'], ['OK']):
        raise ValueError('ELM setup')

  async def command(self, command):
    if command not in COMMANDS:
      raise ValueError('command not allowed')
    if self.queue.overflow or not self.queue.empty():
      raise ValueError('unsolicited ELM data')
    deadline = time.monotonic() + 3
    await self.call(self.uart, GATT, 'WriteValue', 'aya{sv}', ((command + '\r').encode(), {'type': ('s', 'request')}), timeout=2)
    buffer = bytearray()
    for _ in range(64):
      message = await asyncio.wait_for(self.queue.get(), max(0, deadline - time.monotonic()))
      if self.queue.overflow:
        raise ValueError('notification overflow')
      interface, changed, _invalidated = message.body
      if interface != GATT or 'Value' not in changed:
        continue
      value = bytes(changed['Value'][1])
      if len(value) > 512 or len(buffer) + len(value) > 4096:
        raise ValueError('ELM overflow')
      buffer.extend(value)
      if b'>' in buffer:
        if buffer.count(b'>') != 1 or not buffer.endswith(b'>'):
          raise ValueError('ELM trailing data')
        return bytes(buffer)
    raise ValueError('notification limit')

  async def close(self):
    if self.notifying:
      with suppress(Exception):
        await self.call(self.uart, GATT, 'StopNotify', timeout=0.5)
    if self.filter:
      self.filter.__exit__(None, None, None)
    if self.scan:
      with suppress(Exception):
        await self.call(self.scan, ADAPTER, 'StopDiscovery', timeout=0.5)
    # Only our dedicated OBD target, never other Bluetooth devices or BlueZ.
    if self.device:
      with suppress(Exception):
        await self.call(self.device, DEVICE, 'Disconnect', timeout=0.5)


async def run():
  from jeepney.io.asyncio import open_dbus_router
  loop = asyncio.get_running_loop()
  deadline = loop.time() + RETRY_WINDOW
  while True:
    if deadline is None:
      deadline = loop.time() + RECOVERY_WINDOW
    if loop.time() >= deadline:
      write_snapshot('retry_exhausted')
      # Stay alive: manager restart_if_crash must not replenish this budget.
      # The next manager-controlled offroad/onroad start gets a new window.
      await asyncio.Event().wait()
    try:
      address, confirmed = configuration(read_json(CONFIG))
      identity = config_id((address, confirmed))
      write_snapshot('connecting', identity=identity)
      # Includes discovery, connection, setup and the first complete poll.
      # Individual D-Bus timeouts cannot extend the overall retry window.
      async with asyncio.timeout_at(deadline) as startup_timeout:
        async with open_dbus_router('SYSTEM') as router:
          link = Link(router, address)
          try:
            await link.connect()
            trend = TemperatureTrend()
            # Configuration is immutable for this session. A config change causes
            # a clean reconnect before any further poll or publication.
            while configuration(read_json(CONFIG)) == (address, confirmed):
              observed_at, candidates = await poll_metrics(link)
              change = trend.update(observed_at, candidates.get('max_temp_c'))
              if change is not None:
                candidates['temp_change_5min_c'] = change
              if configuration(read_json(CONFIG)) != (address, confirmed):
                break
              write_snapshot('receiving', candidates, confirmed, identity=identity, observed_at=observed_at)
              startup_timeout.reschedule(None)
              # Only a complete validated poll earns a fresh recovery budget.
              # Bluetooth connection/setup alone must not extend failed retries.
              deadline = None
              await asyncio.sleep(PERIOD)
          finally:
            write_snapshot('disconnected', identity=identity)
            # Deadline/cancellation may require up to two more seconds to clean
            # up this target; no further connection or polling starts here.
            with suppress(TimeoutError):
              await asyncio.wait_for(link.close(), 2.0)
    except asyncio.CancelledError:
      raise
    except Exception:
      # No credentials, raw frames, or unbounded exception/log loop.
      write_snapshot('unavailable')
      if deadline is None:
        deadline = loop.time() + RECOVERY_WINDOW
      # Five seconds after failure/cleanup, never overlapping attempts.
      await asyncio.sleep(min(RETRY_INTERVAL, max(0.0, deadline - loop.time())))


def main():
  import fcntl
  os.umask(0o077)
  os.nice(19)
  os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  # Process exclusivity also protects the fixed snapshot temporary filename.
  with open('/dev/shm/carrot-obd-battery.lock', 'w') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return
    async def managed():
      task = asyncio.current_task()
      loop = asyncio.get_running_loop()
      for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
      try:
        await run()
      finally:
        write_snapshot('stopped')
    with suppress(asyncio.CancelledError):
      asyncio.run(managed())


if __name__ == '__main__':
  main()
