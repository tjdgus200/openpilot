"""Opt-in, single-device BLE battery reader; never used by vehicle controls.

The HKMC layouts are candidates for PV5. Owner-enabled charging detail fields
are displayed as unverified; scalar confirmation remains separate. No general-purpose ELM/UDS API.
"""
import asyncio
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
PERIOD = 1.0
POWER_PERIOD = 0.5
RETRY_INTERVAL = 5.0
ATTEMPT_TIMEOUT = 60.0
REQUEST_GAP_MIN = 0.05
REQUEST_GAP_MAX = 0.5
FIELDS = frozenset(('max_temp_c', 'min_temp_c', 'pack_power_kw', 'cell_delta_mv', 'coolant_inlet_c',
                    'soc_percent', 'thermal_mode', 'temp_change_5min_c', 'pack_voltage_v', 'pack_current_a',
                    'charge_mode', 'cell_voltages_v', 'module_temps_c'))
# Owner explicitly enabled these display-only observations before vehicle
# comparison. Never rewrite confirmed_fields or represent them as calibrated.
CHARGING_PREVIEW_FIELDS = frozenset(('charge_mode', 'cell_voltages_v', 'module_temps_c',
                                     'pack_voltage_v', 'pack_current_a'))
REPLIES = {'220101': (62, b'\x62\x01\x01'), '220105': (46, b'\x62\x01\x05'), '220106': (38, b'\x62\x01\x06')}
# Only the four blocks actually observed before the PV5 metadata/zero tail.
CELL_BLOCKS = (('220102', 32), ('220103', 32), ('220104', 32), ('22010A', 12))
REPLIES.update({cmd: (39, bytes.fromhex('62' + cmd[2:])) for cmd, _ in CELL_BLOCKS})
SETUP = ('ATE0', 'ATL0', 'ATM0', 'ATSP6', 'ATCAF1', 'ATCFC1', 'ATFCSM0',
         'ATSH7E4', 'ATCRA7EC', 'ATH1', 'ATD0', 'ATS0', 'ATAL', 'ATST96')
COMMANDS = frozenset((*SETUP, 'ATI', *REPLIES))


class QueryError(ValueError):
  """Bounded local failure category; never contains replies or credentials."""
  def __init__(self, code):
    self.code = code
    super().__init__(code)


def no_data(reply):
  return (isinstance(reply, bytes) and len(reply) <= 4096
          and re.fullmatch(rb'[ \r\n]*NO DATA[ \r\n]*>', reply) is not None)


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
  return hashlib.sha256(json.dumps([address, sorted(fields), 'charging-preview-v1']).encode()).hexdigest()


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
  # The other-model D9 mode map disagreed with PV5 conditioning observed by
  # the owner. Do not label an active 5.5kW conditioning event as idle, or
  # derive thermal consumption from total pack power. Keep this unavailable
  # until PV5 mode/power observations establish the actual source.
  return values


def soc_candidate(payload):
  """Ioniq 5 0105 AF/2: display SOC candidate, not 0101 BMS/internal SOC."""
  if len(payload) != 46 or payload[:3] != b'\x62\x01\x05':
    raise ValueError('SOC payload layout')
  value = payload[34] / 2
  return {'soc_percent': value} if value <= 100 else {}


def charge_candidate(payload):
  """Other-model 0101 J bit7 active, bits5/6 AC/DC; needs PV5 comparison.

  A plug alone is not charging. Conflicting port bits remain unavailable.
  Never infer this from power sign or automatically confirm the candidate.
  """
  if len(payload) != 62 or payload[:3] != b'\x62\x01\x01':
    raise ValueError('charge payload layout')
  flags = payload[12]
  if not flags & 0x80:
    return {'charge_mode': 'off'}
  ports = flags & 0x60
  return {'charge_mode': {0x20: 'ac', 0x40: 'dc'}[ports]} if ports in (0x20, 0x40) else {}


def temperature_array(first, fifth):
  if (len(first) != 62 or first[:3] != b'\x62\x01\x01'
      or len(fifth) != 46 or fifth[:3] != b'\x62\x01\x05'):
    raise ValueError('temperature array layout')
  # 5 + 7 + 3 observed sensors. Slot16 (0105 byte45) was padding in every log sample.
  raw = first[19:24] + fifth[12:19] + fifth[42:45]
  values = [int.from_bytes(bytes((v,)), 'big', signed=True) for v in raw]
  # A real temperature may be zero or below zero. Never truncate at 0 C.
  return [v if -40 <= v <= 90 else None for v in values]


def cell_array(blocks):
  values = []
  for command, count in CELL_BLOCKS:
    payload = blocks[command]
    if len(payload) != 39 or payload[:3] != REPLIES[command][1]:
      raise ValueError('cell array layout')
    # Discard metadata after channel108. Invalid interior readings are holes,
    # not a new cell count; never slide a later channel into an earlier index.
    values.extend(v / 50 if 100 <= v <= 225 else None for v in payload[7:7+count])
  return values


class MetricPoller:
  """One sequential request stream; fixed-size values and per-field timestamps."""
  def __init__(self, details=False):
    self.values, self.observed = {}, {}
    self.due = dict.fromkeys(('220101', '220105', '220106'), 0.)
    self.last_core = None
    self.power_limited = False
    self.first_temperature_page = None
    self.cells_allowed = False
    self.read_seconds = 0.
    self.next_cells = 0.0
    self.details = details
    self.cell_blocks, self.cell_started = {}, 0.0
    self.retry_at, self.failures = {}, {}
    self.last_command, self.last_result, self.no_data_count = None, None, 0
    self.last_no_data_command = None

  def snapshot(self):
    now = time.monotonic()
    for name in tuple(self.observed):
      if not 0 <= now - self.observed[name] <= 5:
        self.observed.pop(name, None)
        self.values.pop(name, None)
    return max(self.observed.values(), default=now), self.values.copy(), self.observed.copy()

  async def read(self, link, command):
    if time.monotonic() < self.retry_at.get(command, 0.):
      return False, None
    self.last_command = command
    self.last_result = 'pending'
    raw = await link.command(command)
    if no_data(raw):
      failures = min(self.failures.get(command, 0) + 1, 5)
      self.failures[command] = failures
      self.retry_at[command] = time.monotonic() + min(30., 2. ** failures)
      self.no_data_count = min(65535, self.no_data_count + 1)
      self.last_no_data_command = command
      self.last_result = 'no_data'
      return True, None
    value = parse_reply(raw, command)
    self.retry_at.pop(command, None)
    self.failures.pop(command, None)
    self.last_result = 'ok'
    return True, value

  def next_core(self, now):
    ready = [cmd for cmd in self.due if now >= max(self.due[cmd], self.retry_at.get(cmd, 0.))]
    others = [cmd for cmd in ready if cmd != '220101']
    # Power first, but a slow power transaction cannot starve every other DID.
    if '220101' in ready and (self.last_core != '220101' or not self.power_limited or not others):
      return '220101'
    return min(others, key=lambda cmd: self.due[cmd], default=None)

  def cell_due(self, now, ready_at=0.):
    command, _ = CELL_BLOCKS[len(self.cell_blocks)]
    due = max(now, ready_at, self.next_cells, self.retry_at.get(command, 0.))
    power_due = max(self.due['220101'], self.retry_at.get('220101', 0.))
    # Use the previous completed transaction as a conservative slot estimate.
    # It is not a hard prediction: every response remains non-preemptive.
    if power_due > now and due + self.read_seconds > power_due:
      due = max(due, power_due)
    return due

  def delay(self, link):
    now = time.monotonic()
    next_due = min(max(due, self.retry_at.get(cmd, 0.)) for cmd, due in self.due.items())
    if self.details and self.cells_allowed:
      next_due = min(next_due, self.cell_due(now, getattr(link, 'next_request', 0.)))
    # Sleep to the next opportunity, not a fixed half-second after a group.
    # The cap also checks vehicle/config changes while optional data is idle.
    return max(REQUEST_GAP_MIN, min(POWER_PERIOD, max(next_due, getattr(link, 'next_request', 0.)) - now))

  def park_allowed(self, parked):
    self.cells_allowed = self.details and (parked() if callable(parked) else parked is True)
    if not self.cells_allowed:
      self.values.pop('cell_voltages_v', None)
      self.observed.pop('cell_voltages_v', None)
      self.cell_blocks.clear()
    return self.cells_allowed

  async def poll(self, link, parked=False, publish=None):
    """Complete at most one transaction, then reselect priority on the next call."""
    self.park_allowed(parked)
    now = time.monotonic()
    if self.cell_blocks and now - self.cell_started > 2.5:
      self.cell_blocks.clear()
      # A busy link defers the optional generation, never extends its age bound.
      self.next_cells = now + 2.
    if now < getattr(link, 'next_request', 0.):
      return self.snapshot()
    command = self.next_core(now)
    if command is None:
      if not self.cells_allowed or now < self.cell_due(now):
        return self.snapshot()
      command, _ = CELL_BLOCKS[len(self.cell_blocks)]
      if now < self.retry_at.get(command, 0.):
        return self.snapshot()
      if not self.cell_blocks:
        self.cell_started = now
    stamp = time.monotonic()
    attempted, reply = await self.read(link, command)
    self.read_seconds = max(0., time.monotonic() - stamp)
    if command in self.due:
      self.last_core = command
      self.due[command] = stamp + (POWER_PERIOD if command == '220101' else PERIOD)
      if command == '220101':
        self.power_limited = max(time.monotonic() + REQUEST_GAP_MIN, getattr(link, 'next_request', 0.)) >= self.due[command]
        self.first_temperature_page = (stamp, reply) if reply is not None else None
      if reply is not None:
        if command == '220101':
          values = candidate_metrics(reply) | charge_candidate(reply)
          fields = ('pack_power_kw', 'max_temp_c', 'min_temp_c', 'cell_delta_mv',
                    'pack_voltage_v', 'pack_current_a', 'charge_mode')
          if 'pack_power_kw' in values:
            data = reply[3:]
            values.update(pack_voltage_v=int.from_bytes(data[12:14], 'big') / 10,
                          pack_current_a=int.from_bytes(data[10:12], 'big', signed=True) / 10)
        elif command == '220105':
          values, fields = soc_candidate(reply), ('soc_percent',)
          first = self.first_temperature_page
          if first is not None and 0 <= time.monotonic() - first[0] <= 1.:
            values['module_temps_c'] = temperature_array(first[1], reply)
            fields += ('module_temps_c',)
        else:
          values, fields = coolant_candidate(reply), ('coolant_inlet_c', 'thermal_mode')
        for name in fields:
          self.values.pop(name, None)
          self.observed.pop(name, None)
          if name in values:
            self.values[name], self.observed[name] = values[name], stamp
        if 'module_temps_c' in values:
          self.observed['module_temps_c'] = min(self.first_temperature_page[0], stamp)
    else:
      # Consume fresh vehicle state again after a slow response, before publishing.
      safe_parked = self.park_allowed(parked)
      if safe_parked and reply is None:
        self.cell_blocks.clear()
        self.next_cells = self.retry_at.get(command, time.monotonic() + 2.)
      elif safe_parked:
        self.cell_blocks[command] = reply
        if len(self.cell_blocks) == len(CELL_BLOCKS):
          if time.monotonic() - self.cell_started <= 2.5:
            self.values['cell_voltages_v'] = cell_array(self.cell_blocks)
            self.observed['cell_voltages_v'] = self.cell_started
          self.cell_blocks.clear()
          # Limit background scan starts even when replies become very fast.
          self.next_cells = max(self.cell_started + 4., time.monotonic() + 2.)
    if attempted and publish is not None:
      publish(*self.snapshot())
    return self.snapshot()


def write_snapshot(state, candidates=None, confirmed=frozenset(), path=SNAPSHOT, identity=None, observed_at=None, field_times=None,
                   diagnostics=None):
  candidates = candidates or {}
  displayed = confirmed | CHARGING_PREVIEW_FIELDS
  record = {'version': 1, 'updated': time.monotonic() if observed_at is None else observed_at, 'state': state, 'config_id': identity,
            'candidates': candidates, 'metrics': {k: v for k, v in candidates.items() if k in displayed},
            'unverified_fields': sorted((CHARGING_PREVIEW_FIELDS - confirmed) & candidates.keys())}
  if field_times is not None:
    record['field_times'] = field_times
  if diagnostics is not None:
    record['collection'] = diagnostics
  raw = json.dumps(record, allow_nan=False, separators=(',', ':')).encode()
  if len(raw) > 4096:
    raise ValueError('snapshot size')
  temporary = path.with_suffix('.tmp')
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, 'wb') as stream:
    stream.write(raw)
  os.replace(temporary, path)


def snapshot_packet(record, now, expected_identity=None):
  """Preserve explicit preview provenance and per-field age across USB."""
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
  field_times = record.get('field_times')
  if field_times is not None and (not isinstance(field_times, dict) or len(field_times) > len(FIELDS)):
    return packet_from_metrics({}, now)
  metrics = {k: (v, stamp if field_times is None else field_times.get(k))
             for k, v in values.items() if k in FIELDS and valid_value(k, v)}
  if metrics.get('min_temp_c', (-50,))[0] > metrics.get('max_temp_c', (130,))[0]:
    metrics.pop('min_temp_c', None)
    metrics.pop('max_temp_c', None)
  pending = record.get('unverified_fields', [])
  if (not isinstance(pending, list) or len(pending) > len(CHARGING_PREVIEW_FIELDS)
      or any(not isinstance(k, str) or k not in CHARGING_PREVIEW_FIELDS for k in pending)):
    return packet_from_metrics({}, now)
  return packet_from_metrics(metrics, now, 'receiving' if metrics else 'profile_required', unverified=pending)


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
    self.request_gap = REQUEST_GAP_MIN
    self.next_request = 0.
    self.last_latency_ms = 0.

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
    delay = max(0., self.next_request - time.monotonic())
    if delay:
      await asyncio.sleep(delay)
    if self.queue.overflow or not self.queue.empty():
      raise QueryError('unsolicited ELM data')
    started = time.monotonic()
    deadline = started + 3
    try:
      await self.call(self.uart, GATT, 'WriteValue', 'aya{sv}', ((command + '\r').encode(), {'type': ('s', 'request')}), timeout=2)
    except TimeoutError:
      raise QueryError('gatt_write_timeout') from None
    except Exception:
      raise QueryError('gatt_write_failed') from None
    buffer = bytearray()
    for _ in range(64):
      try:
        message = await asyncio.wait_for(self.queue.get(), max(0, deadline - time.monotonic()))
      except TimeoutError:
        raise QueryError('elm_prompt_timeout') from None
      if self.queue.overflow:
        raise QueryError('notification overflow')
      interface, changed, _invalidated = message.body
      if interface != GATT or 'Value' not in changed:
        continue
      value = bytes(changed['Value'][1])
      if len(value) > 512 or len(buffer) + len(value) > 4096:
        raise QueryError('ELM overflow')
      buffer.extend(value)
      if b'>' in buffer:
        if buffer.count(b'>') != 1 or not buffer.endswith(b'>'):
          raise QueryError('ELM trailing data')
        reply = bytes(buffer)
        finished = time.monotonic()
        self.last_latency_ms = round((finished - started) * 1000., 1)
        target = max(REQUEST_GAP_MIN, min(REQUEST_GAP_MAX, (finished - started) * .25))
        self.request_gap = min(REQUEST_GAP_MAX, max(target, self.request_gap * .9))
        if command in REPLIES and no_data(reply):
          self.request_gap = min(REQUEST_GAP_MAX, max(.1, self.request_gap * 2.))
        self.next_request = finished + self.request_gap
        return reply
    raise QueryError('notification limit')

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
  while True:
    link = poller = None
    try:
      address, confirmed = configuration(read_json(CONFIG))
      identity = config_id((address, confirmed))
      write_snapshot('connecting', identity=identity)
      # Each attempt is bounded, but failures never exhaust the onroad retry
      # allowance. Manager still stops this display-only reader offroad.
      async with asyncio.timeout(ATTEMPT_TIMEOUT) as startup_timeout:
        async with open_dbus_router('SYSTEM') as router:
          link = Link(router, address)
          try:
            await link.connect()
            poller = MetricPoller()
            details = 'charge_mode' in (confirmed | CHARGING_PREVIEW_FIELDS)
            monitor = None
            if details:
              monitor = vehicle_monitor()
              poller.details = True
            def publish(observed_at, candidates, field_times,
                        session=(address, confirmed, identity, link, poller, startup_timeout)):
              target, fields, source_id, transport, reader, timeout = session
              if configuration(read_json(CONFIG)) != (target, fields):
                raise ValueError('configuration changed')
              write_snapshot('receiving' if candidates else 'waiting_data', candidates, fields, identity=source_id,
                             observed_at=observed_at, field_times=field_times, diagnostics=collection_info(transport, reader))
              # Even a complete NO DATA prompt proves that this ELM session is
              # responsive; it does not make an absent battery field valid.
              timeout.reschedule(None)
            # Configuration is immutable for this session. A config change causes
            # a clean reconnect before any further poll or publication.
            while configuration(read_json(CONFIG)) == (address, confirmed):
              if monitor is not None:
                await poller.poll(link, parked=lambda monitor=monitor: current_parked_state(monitor), publish=publish)
              else:
                await poller.poll(link, publish=publish)
              # No overlapping requests and no catch-up burst after a slow poll.
              await asyncio.sleep(poller.delay(link))
          finally:
            write_snapshot('disconnected', identity=identity)
            # Deadline/cancellation may require up to two more seconds to clean
            # up this target; no further connection or polling starts here.
            with suppress(TimeoutError):
              await asyncio.wait_for(link.close(), 2.0)
    except asyncio.CancelledError:
      raise
    except Exception as exc:
      # No credentials, raw frames, or unbounded exception/log loop.
      code = exc.code if isinstance(exc, QueryError) else 'timeout' if isinstance(exc, TimeoutError) else 'response_or_setup_failed'
      write_snapshot('unavailable', diagnostics=collection_info(link, poller) | {'failure': code})
      # Five seconds after failure/cleanup, never overlapping attempts.
      await asyncio.sleep(RETRY_INTERVAL)


def collection_info(link, poller):
  return {'request': getattr(poller, 'last_command', None), 'result': getattr(poller, 'last_result', None),
          'no_data_count': getattr(poller, 'no_data_count', 0),
          'no_data_request': getattr(poller, 'last_no_data_command', None),
          'request_gap_ms': round(getattr(link, 'request_gap', REQUEST_GAP_MIN) * 1000., 1),
          'response_ms': getattr(link, 'last_latency_ms', 0.)}


def vehicle_monitor():
  from openpilot.cereal import messaging
  return messaging.SubMaster(['carState'])


def current_parked_state(monitor):
  # BLE requests may outlast the vehicle freshness window. Consume the latest
  # carState only when the optional request is about to be admitted; never
  # extend the freshness limit or treat an old P indication as current.
  try:
    monitor.update(0)
  except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
    return False
  return parked_state(monitor, time.monotonic())


def parked_state(monitor, now):
  """Read-only freshness gate for optional cell requests, never vehicle controls."""
  try:
    car = monitor['carState']
    speed = car.vEgo
    return (monitor.valid['carState'] and monitor.alive['carState']
            and 0 <= now - monitor.recv_time['carState'] <= .3
            and str(car.gearShifter) == 'park' and finite_number(speed) and abs(speed) <= .01)
  except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
    return False


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
