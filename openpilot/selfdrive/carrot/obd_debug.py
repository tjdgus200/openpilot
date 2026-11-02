"""Temporary Park-only raw DID observations. No decoded metric or control input."""
import json
import math
import os
from pathlib import Path
import re
import time

from openpilot.selfdrive.carrot.obd_battery import SETUP, QueryError, no_data

REQUEST = Path('/dev/shm/carrot-obd-debug-request.json')
STATUS = Path('/dev/shm/carrot-obd-debug.json')
MAX_SECONDS = 600
PERIOD = .5  # At most two complete DID transactions/s, no catch-up.
# Only source/DID/length combinations actually observed in owner PV5 logs.
# D0 below always excludes 62 + the two DID bytes. Meanings remain unknown.
TARGETS = (
  ('7EC/0101', '7E4', '7EC', '220101', 62),
  ('7EC/0106', '7E4', '7EC', '220106', 38),
  ('7EC/0114', '7E4', '7EC', '220114', 33),
  ('7BB/0100', '7B3', '7BB', '220100', 39),
  ('7BB/0101', '7B3', '7BB', '220101', 39),
  ('7BB/0102', '7B3', '7BB', '220102', 39),
)
CONTRACTS = {key: (tx, rx, cmd, length) for key, tx, rx, cmd, length in TARGETS}
REQUEST_LENGTHS = {(rx, cmd): length for _, _, rx, cmd, length in TARGETS}


class ModeChanged(Exception):
  """Lease or vehicle proof revoked; no new diagnostic write may start."""


def finite(value):
  return type(value) in (int, float) and math.isfinite(value)


def read(path, limit=4096):
  with path.open('rb') as stream:
    raw = stream.read(limit + 1)
  if len(raw) > limit:
    raise ValueError('debug size')
  return json.loads(raw)


def atomic_write(path, value):
  raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode('ascii')
  if len(raw) > 4096:
    raise ValueError('debug size')
  temporary = path.with_suffix('.tmp')
  flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
  fd = os.open(temporary, flags, 0o600)
  with os.fdopen(fd, 'wb') as stream:
    stream.write(raw)
  os.replace(temporary, path)


def valid_lease(value, identity, now):
  if (not isinstance(value, dict) or set(value) != {'version', 'config_id', 'nonce', 'start', 'until'}
      or type(value['version']) is not int or value['version'] != 1 or value['config_id'] != identity
      or not isinstance(value['nonce'], str) or re.fullmatch('[0-9a-f]{16}', value['nonce']) is None):
    return False
  start, until = value['start'], value['until']
  return (finite(now) and finite(start) and finite(until) and 0 <= start <= now < until
          and 0 < until - start <= MAX_SECONDS)


def parked(monitor, now):
  try:
    if not all(monitor.valid[n] and monitor.alive[n] and 0 <= now - monitor.recv_time[n] < .25
               for n in ('carState', 'carControl', 'selfdriveState')):
      return False
    car = monitor['carState']
    return (car.canValid is True and car.canTimeout is False and str(car.gearShifter) == 'park'
            and finite(car.vEgo) and finite(car.vEgoRaw) and abs(car.vEgo) <= .01 and abs(car.vEgoRaw) <= .01
            and monitor['carControl'].enabled is False and monitor['carControl'].latActive is False
            and monitor['carControl'].longActive is False and monitor['selfdriveState'].active is False
            and monitor['selfdriveState'].enabled is False)
  except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
    return False


class Control:
  def __init__(self, path=REQUEST):
    self.path, self.monitor = path, None
    self.next_read, self.lease = 0., None

  def active(self, identity):
    now = time.monotonic()
    if now < self.next_read - .5 or now >= self.next_read:
      self.next_read, self.lease = now + .5, None
      try:
        value = read(self.path, 512)
        if valid_lease(value, identity, now):
          self.lease = value
      except (OSError, ValueError, TypeError, KeyError):
        pass
    if not valid_lease(self.lease, identity, now):
      return None
    try:
      if self.monitor is None:
        from openpilot.cereal import messaging
        self.monitor = messaging.SubMaster(['carState', 'carControl', 'selfdriveState'])
      self.monitor.update(0)
      return self.lease if parked(self.monitor, time.monotonic()) else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
      return None


def diagnostic_request_bytes(command, rx):
  if command in SETUP or command in ('ATI', 'ATSH7B3', 'ATCRA7BB'):
    return (command + '\r').encode('ascii')
  if (rx, command) not in REQUEST_LENGTHS:
    raise ValueError('debug command not allowed')
  # 0101 differs by ECU. Count must match the current receive filter.
  length = REQUEST_LENGTHS[rx, command]
  return (command + format((length + 7) // 7, 'X') + '\r').encode('ascii')


async def switch_mode(link, lease, allowed):
  """Called only between complete transactions by the existing single owner."""
  link.request_guard = None
  link.diagnostic = False
  await link.resynchronize()
  # Restore both even if a prior permission loss interrupted a header change
  # before debug_rx was updated. These local ELM commands do not query an ECU.
  for command in ('ATSH7E4', 'ATCRA7EC'):
    reply = await link.command(command)
    if reply.strip() not in (b'OK\r>', b'OK\r\n>'):
      raise ValueError('ELM setup')
  link.debug_rx = '7EC'
  STATUS.unlink(missing_ok=True)  # noqa: ASYNC240 -- one fixed tmpfs entry; avoid a worker thread
  link.diagnostic = lease is not None
  link.request_guard = allowed if lease is not None else None


def parse(reply, target):
  _, rx, cmd, length = CONTRACTS[target]
  if not isinstance(reply, bytes) or len(reply) > 4096 or reply.count(b'>') != 1 or not reply.endswith(b'>'):
    raise ValueError('debug framing')
  rows = [line.strip().replace(' ', '').upper() for line in reply[:-1].decode('ascii').replace('\n', '\r').split('\r')
          if line.strip()]
  if len(rows) != (length + 7) // 7 or any(re.fullmatch(rx + '[0-9A-F]{16}', line) is None for line in rows):
    raise ValueError('debug source or count')
  frames = [bytes.fromhex(line[3:]) for line in rows]
  if frames[0][:2] != bytes((0x10, length)):
    raise ValueError('debug length')
  payload = bytearray(frames[0][2:])
  for seq, frame in enumerate(frames[1:], 1):
    if frame[0] != (0x20 | seq):
      raise ValueError('debug sequence')
    payload.extend(frame[1:1 + min(7, length - len(payload))])
  if len(payload) != length or payload[:3] != bytes.fromhex('62' + cmd[2:]):
    raise ValueError('debug DID')
  return bytes(payload[3:])


def negative_response(reply, rx):
  # A single framed UDS negative response has no DID payload to display. It
  # cannot identify a particular signal; do not infer a value or meaning.
  if not isinstance(reply, bytes) or len(reply) > 4096 or reply.count(b'>') != 1 or not reply.endswith(b'>'):
    return False
  compact = reply[:-1].strip().replace(b' ', b'').upper()
  if re.fullmatch(rx.encode() + rb'037F22[0-9A-F]{10}', compact) is None:
    return False
  return int(compact[9:11], 16) != 0


class Poller:
  def __init__(self, lease, identity, allowed, path=STATUS):
    self.lease, self.identity, self.allowed, self.path = lease, identity, allowed, path
    self.rows, self.index, self.rx, self.next_poll = {}, 0, '7EC', 0.
    self.resync_streak = 0
    self.last_valid = None

  def publish(self, key, state, data=None):
    self.check()
    now = time.monotonic()
    row = {'state': state, 'at': now}
    if data is not None:
      row['data'] = data.hex().upper()
    self.rows[key] = row  # Replace, never retain bytes from a failed request.
    atomic_write(self.path, {'version': 1, 'nonce': self.lease['nonce'], 'config_id': self.identity,
                             'updated': now, 'rows': self.rows})

  def advance(self):
    self.index = (self.index + 1) % len(TARGETS)
    self.next_poll = time.monotonic() + PERIOD

  def check(self):
    if not self.allowed():
      raise ModeChanged

  async def poll(self, link):
    self.check()
    if time.monotonic() < self.next_poll:
      return
    key, tx, rx, cmd, _ = TARGETS[self.index]
    if self.rx != rx:
      for command in ('ATSH' + tx, 'ATCRA' + rx):
        self.check()
        reply = await link.command(command)
        if reply.strip() != b'OK\r>' and reply.strip() != b'OK\r\n>':
          raise ValueError('ELM setup')
      self.rx = rx
    link.debug_rx = rx
    self.check()
    try:
      reply = await link.command(cmd)
    except QueryError as exc:
      # Only an ECU/UART response deadline is recoverable here. A failed GATT
      # write, overflow or adapter/filter failure still ends the connection.
      if exc.code != 'elm_prompt_timeout':
        raise
      self.publish(key, 'timeout')
      # Never send the next DID into an unfinished response. Retire late bytes
      # and require bounded UART silence plus the exact local ATI identity.
      await link.resynchronize()
      self.check()
      self.resync_streak = 0
      self.advance()
      return True
    self.check()  # A response cannot renew validity after motion/control loss.
    now = time.monotonic()
    if no_data(reply):
      self.resync_streak = 0
      self.publish(key, 'no_data')
    elif negative_response(reply, rx):
      self.publish(key, 'unavailable')
      # This includes response-pending: quarantine any later completion before
      # sending another ID. Never decode it as this or the next page's data.
      await link.resynchronize()
      self.check()
      self.resync_streak = 0
    else:
      try:
        data = parse(reply, key)
      except ValueError:
        if self.resync_streak >= 2:
          raise QueryError('elm_resync_limit') from None
        self.resync_streak += 1
        self.check()
        await link.resynchronize()
        self.check()
        self.publish(key, 'invalid')
      else:
        self.resync_streak = 0
        self.last_valid = now
        self.publish(key, 'ok', data)
    self.advance()
    return True

  def delay(self, link):
    return max(.05, min(.1, self.next_poll - time.monotonic()))


class Display:
  """UI reads only at 1Hz, expiry/vehicle gate is checked at each render."""
  def __init__(self, request=REQUEST, status=STATUS, config=None):
    if config is None:
      from openpilot.selfdrive.carrot.obd_battery import CONFIG
      config = CONFIG
    self.request, self.status, self.config = request, status, config
    self.next_read, self.lease = 0., None
    self.rows, self.previous, self.changed = {}, {}, {}

  def update(self, now, allowed):
    if not allowed or not finite(now) or now < self.next_read - 1.:
      self.lease, self.rows, self.previous, self.changed = None, {}, {}, {}
      self.next_read = 0.
      return None
    if now >= self.next_read:
      self.next_read = now + 1.
      try:
        from openpilot.selfdrive.carrot.obd_battery import config_id, configuration
        lease = read(self.request, 512)
        if not valid_lease(lease, config_id(configuration(read(self.config))), now):
          raise ValueError('debug lease')
        if self.lease != lease:
          self.previous, self.changed = {}, {}
        self.lease, self.rows = lease, {}
      except (OSError, ValueError, TypeError, KeyError, AttributeError):
        self.lease, self.rows, self.previous, self.changed = None, {}, {}, {}
        return None
      try:
        record = read(self.status)
        if (type(record.get('version')) is not int or record['version'] != 1 or record.get('nonce') != lease['nonce']
            or record.get('config_id') != lease['config_id'] or not finite(record.get('updated'))
            or not lease['start'] <= record['updated'] <= now or now - record['updated'] > 5
            or not isinstance(record.get('rows'), dict) or not record['rows'].keys() <= CONTRACTS.keys()):
          raise ValueError('debug snapshot')
        for key, row in record['rows'].items():
          if (not isinstance(row, dict) or row.get('state') not in ('ok', 'no_data', 'invalid', 'timeout', 'unavailable')
              or not finite(row.get('at')) or not lease['start'] <= row['at'] <= record['updated']):
            raise ValueError('debug row')
          if row['state'] == 'ok':
            data = row.get('data')
            if not isinstance(data, str) or re.fullmatch('[0-9A-F]{' + str((CONTRACTS[key][3] - 3) * 2) + '}', data) is None:
              raise ValueError('debug bytes')
            value = bytes.fromhex(data)
            previous = self.previous.get(key)
            if previous is not None and previous[0] < row['at']:
              for index, byte in enumerate(value):
                if previous[1][index] != byte:
                  self.changed[key, index] = row['at']
            self.previous[key] = (row['at'], value)
          self.rows[key] = row
      except (OSError, ValueError, TypeError, KeyError, AttributeError):
        self.rows = {}
    if self.lease is None or not valid_lease(self.lease, self.lease['config_id'], now):
      self.lease, self.rows, self.previous, self.changed = None, {}, {}, {}
      return None
    return self.rows
