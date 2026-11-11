"""Bounded, coded incident queue and authenticated wire format; standard library only."""
import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

MAX_ROWS = 32
MAX_SPOOL = 65536
MAX_BODY = 8192
MAX_BATCH = 12
MAX_COUNT = 1000000
MAX_AGE = 86400
SOURCE = Path(__file__).with_name('known_sources.json')
KNOWN = json.loads(SOURCE.read_text())
ALERTS, PROCESSES = frozenset(KNOWN['alerts']), frozenset(KNOWN['processes'])
HEX32, HEX40, HEX64 = (re.compile(r'[0-9a-f]{%d}' % n) for n in (32, 40, 64))
CONDITIONS = frozenset(('preDriverDistracted', 'promptDriverDistracted', 'driverDistracted',
  'steerSaturated', 'overheat', 'calibrationIncomplete', 'belowEngageSpeed', 'wrongGear',
  'doorOpen', 'seatbeltNotLatched', 'espDisabled', 'wrongCarMode'))


def encode(value):
  return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def coded(raw):
  """Drop all free text/credentials/location/IDs, and accept only known coded facts."""
  if not isinstance(raw, dict) or raw.get('vehicle') != 'KIA_PV5':
    raise ValueError('invalid_vehicle')
  kind, process, code, commit = (raw.get(k) for k in ('kind', 'process', 'code', 'source_commit'))
  if type(process) is not str or process not in PROCESSES:
    raise ValueError('invalid_process')
  if type(commit) is not str or not (HEX40.fullmatch(commit) or commit == 'unknown'):
    raise ValueError('invalid_source')
  if kind == 'process_exit':
    if type(code) is not int or code == 0 or not -128 <= code <= 255:
      raise ValueError('invalid_exit')
  elif kind == 'system_alert':
    if type(code) is not str or code not in ALERTS:
      raise ValueError('invalid_alert')
  else:
    raise ValueError('invalid_kind')
  severity = raw.get('severity')
  if type(severity) is not int or not 1 <= severity <= 3:
    raise ValueError('invalid_severity')
  return {'vehicle': 'KIA_PV5', 'kind': kind, 'process': process, 'code': code,
          'source_commit': commit, 'severity': severity}


@dataclass(frozen=True)
class SpeedProof:
  stamp: float
  valid: bool
  pv5: bool
  can_valid: bool
  can_timeout: bool
  raw_speed: float
  speed: float
  standstill: bool


def stopped(proof, now):
  # Gear and cruise/lateral/longitudinal enable state deliberately do not gate
  # reporting: a fresh true stop in Drive/Reverse/Neutral is authorized too.
  return (isinstance(proof, SpeedProof) and type(now) in (int, float) and math.isfinite(now)
          and proof.valid is True and proof.pv5 is True and proof.can_valid is True
          and proof.can_timeout is False and proof.standstill is True
          and type(proof.stamp) in (int, float) and math.isfinite(proof.stamp)
          and 0 <= now - proof.stamp < .25
          and all(type(v) in (int, float) and math.isfinite(v) for v in (proof.raw_speed, proof.speed))
          and proof.raw_speed == 0 and abs(proof.speed) <= .01)


def row_valid(row):
  if not isinstance(row, dict) or set(row) != {'id', 'revision', 'count', 'first', 'last', 'fact'}:
    raise ValueError('invalid_row')
  if type(row['id']) is not str or not HEX32.fullmatch(row['id']):
    raise ValueError('invalid_id')
  if type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_COUNT:
    raise ValueError('invalid_revision')
  if type(row['count']) is not int or row['count'] != row['revision']:
    raise ValueError('invalid_count')
  if any(type(row[k]) is not int or not 0 <= row[k] <= 10000000000 for k in ('first', 'last')) or row['first'] > row['last']:
    raise ValueError('invalid_time')
  if coded(row['fact']) != row['fact']:
    raise ValueError('extra_fact_fields')
  return row


class Pending:
  def __init__(self):
    self.rows = {}
    self.evicted = 0
    self.rejected = 0

  def add(self, raw, now):
    try:
      fact = coded(raw)
      if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 10000000000:
        raise ValueError('invalid_time')
    except (ValueError, TypeError, OverflowError):
      self.rejected = min(MAX_COUNT, self.rejected + 1)
      return
    key = hashlib.sha256(encode(fact)).hexdigest()
    row = self.rows.get(key)
    if row is None:
      if len(self.rows) == MAX_ROWS:
        # Oldest lowest severity first. Loss remains visible as a counter.
        victim = min(self.rows, key=lambda k: (self.rows[k]['fact']['severity'], self.rows[k]['last']))
        if fact['severity'] < self.rows[victim]['fact']['severity']:
          self.evicted = min(MAX_COUNT, self.evicted + 1)
          return
        del self.rows[victim]
        self.evicted = min(MAX_COUNT, self.evicted + 1)
      self.rows[key] = {'id': secrets.token_hex(16), 'revision': 1, 'count': 1,
                        'first': int(now), 'last': int(now), 'fact': fact}
    else:
      row['count'] = row['revision'] = min(MAX_COUNT, row['revision'] + 1)
      row['last'] = max(row['last'], int(now))

  def expire(self, now):
    for key, row in list(self.rows.items()):
      if now - row['last'] > MAX_AGE or row['last'] - now > 300:
        del self.rows[key]
        self.evicted = min(MAX_COUNT, self.evicted + 1)

  def batch(self, now):
    self.expire(now)
    rows = []
    for row in sorted(self.rows.values(), key=lambda r: (-r['fact']['severity'], r['first']))[:MAX_BATCH]:
      candidate = json.loads(encode(row))
      if len(encode({'format': 1, 'rows': [*rows, candidate], 'evicted': self.evicted, 'rejected': self.rejected})) > MAX_BODY:
        break
      rows.append(candidate)
    return {'format': 1, 'rows': rows, 'evicted': self.evicted, 'rejected': self.rejected}

  def acknowledge(self, sent):
    versions = {(r['id'], r['revision']) for r in sent['rows']}
    for key, row in list(self.rows.items()):
      if (row['id'], row['revision']) in versions:
        del self.rows[key]

  def dump(self):
    data = encode({'format': 1, 'rows': list(self.rows.values()), 'evicted': self.evicted, 'rejected': self.rejected})
    if len(data) > MAX_SPOOL:
      raise ValueError('spool_too_large')
    return data

  def restore(self, data, now):
    if len(data) > MAX_SPOOL:
      raise ValueError('spool_too_large')
    value = json.loads(data)
    if (not isinstance(value, dict) or set(value) != {'format', 'rows', 'evicted', 'rejected'}
        or value.get('format') != 1 or not isinstance(value.get('rows'), list) or len(value['rows']) > MAX_ROWS):
      raise ValueError('invalid_spool')
    restored = {}
    ids = set()
    for row in value['rows']:
      row_valid(row)
      key = hashlib.sha256(encode(row['fact'])).hexdigest()
      if row['id'] in ids or key in restored:
        raise ValueError('duplicate_spool_row')
      ids.add(row['id'])
      restored[key] = row
    for key in ('evicted', 'rejected'):
      number = value.get(key, 0)
      if type(number) is not int or not 0 <= number <= MAX_COUNT:
        raise ValueError('invalid_counters')
    self.rows = restored
    self.evicted, self.rejected = value['evicted'], value['rejected']
    self.expire(now)


def validate_batch(data):
  if not isinstance(data, bytes) or len(data) > MAX_BODY:
    raise ValueError('invalid_size')
  obj = json.loads(data)
  if not isinstance(obj, dict) or set(obj) != {'format', 'rows', 'evicted', 'rejected'} or obj['format'] != 1:
    raise ValueError('invalid_envelope')
  if not isinstance(obj['rows'], list) or not 1 <= len(obj['rows']) <= MAX_BATCH:
    raise ValueError('invalid_rows')
  if any(type(obj[k]) is not int or not 0 <= obj[k] <= MAX_COUNT for k in ('evicted', 'rejected')):
    raise ValueError('invalid_counters')
  ids = set()
  for row in obj['rows']:
    row_valid(row)
    if row['id'] in ids:
      raise ValueError('duplicate_row')
    ids.add(row['id'])
  return obj


def signature(key, stamp, nonce, data):
  if not isinstance(key, bytes) or len(key) != 32:
    raise ValueError('invalid_key')
  signed = (str(stamp) + '\n' + nonce + '\n').encode() + data
  return hmac.new(key, signed, hashlib.sha256).hexdigest()


def authenticated(key, stamp, nonce, digest, data, now):
  if type(stamp) is not int or abs(now - stamp) > 300 or type(nonce) is not str or not HEX32.fullmatch(nonce):
    return False
  if type(digest) is not str or not HEX64.fullmatch(digest):
    return False
  return hmac.compare_digest(signature(key, stamp, nonce, data), digest)


class Retry:
  def __init__(self):
    self.next_at = 0.
    self.failures = 0

  def ready(self, now):
    return now >= self.next_at

  def failed(self, now):
    self.failures = min(9, self.failures + 1)
    self.next_at = now + min(300, 2 ** self.failures)

  def succeeded(self, now):
    self.failures = 0
    self.next_at = now + 5
