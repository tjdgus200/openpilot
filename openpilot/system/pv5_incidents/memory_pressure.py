"""Capture and validate the bounded memory diagnostic shared with the PC receiver."""
import json
from heapq import nlargest

from openpilot.system.pv5_incidents.core import HEX32, HEX40, MAX_BODY, PROCESSES

MEMORY_PROCESS_LIMIT = 16
MEMORY_PROCESS_NAMES = PROCESSES | {'manager', 'python3', 'systemd', 'other'}
MEMORY_SAMPLE_MAX_AGE_MS = 120_000  # Same expiration as the existing procLog sampler.
MEMORY_THRESHOLD = 85
CAPTURE_INTERVAL = 300.0
MAX_LOG_AGE = 5.0
FIXED_FIELDS = {'vehicle': 'KIA_PV5', 'kind': 'memory_pressure', 'process': 'proclogd',
                'code': 'memoryPressure', 'severity': 1}


def unique_fields(pairs):
  value = dict(pairs)
  if len(value) != len(pairs):
    raise ValueError('duplicate_memory_field')
  return value


def validate_memory(data):
  if type(data) is not bytes or not 0 < len(data) <= MAX_BODY:
    raise ValueError('invalid_size')
  value = json.loads(data, object_pairs_hook=unique_fields)
  if (type(value) is not dict or value.keys() != {'format', 'id', 'captured_at', 'fact'}
      or type(value['format']) is not int or value['format'] != 2
      or type(value['id']) is not str or not HEX32.fullmatch(value['id'])
      or type(value['captured_at']) is not int or not 0 <= value['captured_at'] <= 10_000_000_000):
    raise ValueError('invalid_memory_envelope')
  fact = value['fact']
  if (type(fact) is not dict or fact.keys() != FIXED_FIELDS.keys() | {'source_commit', 'usage_percent', 'pss'}
      or type(fact['severity']) is not int or any(fact[k] != v for k, v in FIXED_FIELDS.items())
      or type(fact['source_commit']) is not str or not (fact['source_commit'] == 'unknown' or HEX40.fullmatch(fact['source_commit']))
      or type(fact['usage_percent']) is not int or not MEMORY_THRESHOLD <= fact['usage_percent'] <= 100
      or type(fact['pss']) is not list or not 1 <= len(fact['pss']) <= MEMORY_PROCESS_LIMIT):
    raise ValueError('invalid_memory_fact')
  for sample in fact['pss']:
    if (type(sample) is not dict or sample.keys() != {'name', 'bytes', 'age_ms'}
        or type(sample['name']) is not str or sample['name'] not in MEMORY_PROCESS_NAMES
        or type(sample['bytes']) is not int or not 0 < sample['bytes'] <= 1 << 40
        or type(sample['age_ms']) is not int or not 0 <= sample['age_ms'] <= MEMORY_SAMPLE_MAX_AGE_MS):
      raise ValueError('invalid_memory_sample')
  return value


def fresh(sm, topic, now, age):
  stamp = sm.logMonoTime.get(topic, 0)
  return (sm.valid.get(topic, False) and sm.alive.get(topic, False) and stamp > 0
          and 0 <= now - stamp / 1e9 <= age)


def process_samples(procs, names, now_ns):
  for proc in procs:
    age_ns = now_ns - proc.memPssMonoTime
    if not (proc.memPssMonoTime > 0 and 0 <= age_ns <= MEMORY_SAMPLE_MAX_AGE_MS * 1_000_000
            and 0 < proc.memPss <= 1 << 40):
      continue
    name = names.get(proc.pid, proc.name)
    yield {'name': name if name in MEMORY_PROCESS_NAMES else 'other',
           'bytes': proc.memPss, 'age_ms': age_ns // 1_000_000}


class MemoryPressure:
  def __init__(self):
    self.next_capture = 0.0
    self.last_log = 0

  def due(self, device, now):
    return (now >= self.next_capture and fresh(device, 'deviceState', now, MAX_LOG_AGE)
            and MEMORY_THRESHOLD <= device['deviceState'].memoryUsagePercent <= 100)

  def observe(self, sm, now, commit, device=None):
    device = sm if device is None else device
    if not self.due(device, now) or not fresh(sm, 'procLog', now, MAX_LOG_AGE):
      return None
    stamp = sm.logMonoTime['procLog']
    if stamp == self.last_log:
      return None
    self.last_log = stamp
    names = {}
    if fresh(sm, 'managerState', now, MAX_LOG_AGE):
      names = {process.pid: process.name for process in sm['managerState'].processes
               if process.running and process.name in MEMORY_PROCESS_NAMES}
    samples = nlargest(MEMORY_PROCESS_LIMIT, process_samples(sm['procLog'].procs, names, int(now * 1e9)),
                       key=lambda sample: sample['bytes'])
    if not samples:
      return None
    self.next_capture = now + CAPTURE_INTERVAL
    return {**FIXED_FIELDS, 'source_commit': commit, 'usage_percent': device['deviceState'].memoryUsagePercent, 'pss': samples}
