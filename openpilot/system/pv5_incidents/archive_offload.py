"""Home-only opaque transfer. Filtering/decoding is exclusively on the owner PC."""
import threading
import time
from openpilot.system.pv5_incidents.archive_protocol import PC_RAW, file_id
from openpilot.system.pv5_incidents.archive_parallel import run_parallel


class RawCatalog:
  def __init__(self, raw):
    self.raw = raw

  def next(self, commit, exclude=frozenset()):
    original = self.raw.next(commit, exclude=exclude)
    if original is None:
      return None
    value = dict(original['manifest'], privacy=PC_RAW, files=[])
    sources = {}
    for entry in original['manifest']['files']:
      new = dict(entry, file_id=file_id(self.raw.key, value['segment'], entry['name'], entry['size'], entry['mtime_ns'], PC_RAW))
      sources[new['file_id']] = entry
      value['files'].append(new)
    return {'manifest': value, 'original': original, 'sources': sources}

  def read(self, candidate, entry, offset, size):
    return self.raw.read(candidate['original'], candidate['sources'][entry['file_id']], offset, size)

  def completed(self, candidate):
    self.raw.completed(candidate['original'])


class One:
  def __init__(self, catalog, candidate):
    self.catalog, self.candidate = catalog, candidate
    self.sent = False

  def next(self, commit):
    if self.sent:
      return None
    self.sent = True
    return self.candidate

  def read(self, *args):
    return self.catalog.read(*args)

  def completed(self, candidate):
    pass  # Only the scheduling owner commits the durable cursor after all ACKs.


class Pacer:
  def __init__(self, rate=8 * 1024**2, clock=time.monotonic, sleep=time.sleep):
    self.rate, self.clock, self.sleep = rate, clock, sleep
    self.started, self.count, self.lock = clock(), 0, threading.Lock()

  def __call__(self, count, check):
    with self.lock:
      self.count += count
      due = self.started + self.count / self.rate
    while self.clock() < due:
      check()
      self.sleep(min(.05, due - self.clock()))


def upload(catalogs, target, key, allowed, profiles, commit, workers=4, transport_factory=None, profile=None):
  from openpilot.system.pv5_incidents.archive_worker import Copier, Transport, Paused, wifi_profile
  transport_factory = transport_factory or Transport
  profile = profile or wifi_profile
  start, pacer = time.monotonic(), Pacer()
  def admitted():
    return allowed() is True and time.monotonic() - start < 300
  total = {'reason': 'idle', 'bytes': 0, 'segments': 0}
  for raw in catalogs:
    catalog = RawCatalog(raw)
    def transfer(candidate, continuing):
      transport = transport_factory(target, key, continuing, timeout=30)
      request = transport.request
      transport.request = lambda path, body: request(path.replace('/v1/archive/', '/v1/archive/raw/', 1), body)
      copier = Copier(One(catalog, candidate), transport, continuing, profiles, profile=profile,
                      home_unlimited=True, pacer=pacer)
      copier.start = start
      return copier.run(commit)['bytes']
    if not admitted():
      raise Paused('budget' if allowed() else 'state')
    result = run_parallel(catalog, transfer, commit, admitted, workers)
    total['bytes'] += result['bytes']
    total['segments'] += result['segments']
  return total
