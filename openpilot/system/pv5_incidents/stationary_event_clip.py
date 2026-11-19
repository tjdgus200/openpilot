"""Build private, timestamp-bounded diagnostic clips from already closed logs."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from openpilot.system.pv5_incidents.archive_catalog import SourceChanged
from openpilot.system.pv5_incidents.archive_protocol import CHUNK, MAX_FILE, PRIVACY, RLOG_NAMES, SEGMENT, file_id, identifier
from openpilot.system.pv5_incidents.archive_sanitize import BoundedWriter, COPY_LIMIT, identity
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from openpilot.system.pv5_incidents.core import HEX40, PROCESSES
from openpilot.system.pv5_incidents.priority import CRITICAL_ALERTS, REPORTABLE_ALERTS, reportable
from openpilot.system.pv5_incidents.stationary_event_catalog import RecentLogCatalog

PADDING_NS = 5_000_000_000
MAX_WINDOWS = 8
URGENT_CAUSES = frozenset({'stock_aeb', *('alert:' + name for name in CRITICAL_ALERTS)})
WAITING_FOR_CLOSE = object()
DIAGNOSTICS = frozenset((
  'accelerometer', 'cameraOdometry', 'can', 'carControl', 'carOutput', 'carState', 'controlsState',
  'deviceState', 'driverMonitoringState', 'drivingModelData', 'gyroscope', 'lateralManeuverPlan', 'liveCalibration',
  'liveDelay', 'liveParameters', 'livePose', 'liveTorqueParameters', 'liveTracks', 'longitudinalPlan',
  'managerState', 'modelV2', 'onroadEvents', 'pandaStates', 'parkedDashcamState', 'peripheralState', 'radarState',
  'roadCameraState', 'roadEncodeIdx', 'selfdriveState', 'sendcan', 'wideRoadCameraState', 'wideRoadEncodeIdx',
))
PRIVATE_FIELDS = {
  'carParams': ('carVin',),
  'deviceState': ('networkInfo',),
  'roadCameraState': ('image',),
  'wideRoadCameraState': ('image',),
}


@dataclass(frozen=True)
class Source:
  path: Path
  fingerprint: tuple[int, int, int, int]

  def descriptor(self):
    return [self.path.parent.name, self.path.name, list(self.fingerprint)]


def active_causes(event):
  if not event.valid:
    return None
  topic = event.which()
  if topic == 'onroadEvents':
    return {'alert:' + str(value.name) for value in event.onroadEvents
            if str(value.name) in REPORTABLE_ALERTS
            and (value.warning or value.noEntry or value.softDisable or value.immediateDisable or value.permanent)}
  if topic == 'carState':
    state = event.carState
    if state.canValid and not state.canTimeout:
      return {'stock_aeb'} if state.stockAeb else set()
  if topic == 'managerState':
    return {f'exit:{str(value.name)}:{value.pid}:{value.exitCode}' for value in event.managerState.processes
            if str(value.name) in PROCESSES and value.pid > 0 and not value.running and value.shouldBeRunning
            and reportable({'kind': 'process_exit', 'code': int(value.exitCode)})}
  return None


def private_message(event, schema):
  topic = event.which()
  message = schema.Event.new_message(logMonoTime=event.logMonoTime, valid=event.valid)
  if topic == 'initData':
    context = message.init('initData')
    for key in ('gitCommit', 'gitSrcCommit'):
      value = str(getattr(event.initData, key))
      if HEX40.fullmatch(value):
        setattr(context, key, value)
    context.dirty = event.initData.dirty
    context.passive = event.initData.passive
  else:
    fields = getattr(event, topic).to_dict()
    for key in PRIVATE_FIELDS[topic]:
      fields.pop(key, None)
    message.init(topic).from_dict(fields)
  return message.to_bytes()


class ClipCatalog:
  def __init__(self, root, database, key, allowed, check, schema=None, **kwargs):
    if schema is None:
      from openpilot.cereal import log as schema

    self.source = RecentLogCatalog(root, database.with_name('archive-event-clips-v2.sqlite'), key, allowed,
                                   groups=(RLOG_NAMES,), **kwargs)
    self.root, self.key, self.check, self.schema = self.source.root, key, check, schema
    self.cache = database.parent / 'event-clip-cache-v2'
    self.check()
    self.cache.mkdir(mode=0o700, exist_ok=True)
    if self.cache.is_symlink() or not self.cache.is_dir():
      raise ValueError('unsafe_event_clip_cache')
    with self.source.db() as db:
      db.execute('''CREATE TABLE IF NOT EXISTS clips(
        segment TEXT, center INTEGER, first INTEGER, last INTEGER,
        origin TEXT, plan TEXT, token TEXT, PRIMARY KEY(segment,center))''')

  def sources_unchanged(self, sources):
    self.check()
    self.source.check()
    for source in sources:
      if source.path.parent.is_symlink() or self.source.locked(source.path.parent) or identity(source.path) != source.fingerprint:
        raise SourceChanged('event_source_changed')

  def read_frames(self, source):
    self.sources_unchanged((source,))
    with decoded(source.path) as stream:
      yield from messages(stream, self.check)
    self.sources_unchanged((source,))

  def scan(self, source):
    first, last = None, None
    centers, active = {}, {}
    for frame in self.read_frames(source):
      with self.schema.Event.from_bytes(frame, traversal_limit_in_words=2**21) as event:
        topic, stamp = event.which(), event.logMonoTime
        if not 0 < stamp < 2**63 or topic not in DIAGNOSTICS:
          continue
        first = stamp if first is None else min(stamp, first)
        last = stamp if last is None else max(stamp, last)
        causes = active_causes(event)
        if causes is None:
          continue
        new_causes = causes - active.get(topic, set())
        if new_causes:
          rank = 2 if new_causes & URGENT_CAUSES else int(topic == 'managerState')
          centers[stamp] = max(rank, centers.get(stamp, 0))
          if len(centers) > MAX_WINDOWS:
            # Retain the earliest samples when priorities are equal.
            weakest = min(centers, key=lambda center: (centers[center], -center))
            del centers[weakest]
        active[topic] = causes
    return sorted(centers), first, last

  def find(self, name, wait_closed=False):
    self.check()
    folder = self.root / name
    if not folder.is_dir() or folder.is_symlink():
      return None
    if self.source.locked(folder):
      return WAITING_FOR_CLOSE if wait_closed else None
    for basename in RLOG_NAMES:
      path = folder / basename
      try:
        fingerprint = identity(path)
      except FileNotFoundError:
        continue
      if not 0 < fingerprint[2] <= MAX_FILE:
        return None
      if self.source.clock() - fingerprint[3] / 1e9 < 2:
        return WAITING_FOR_CLOSE if wait_closed else None
      return Source(path, fingerprint)
    return None

  def new_plan(self, name, center, first, last, origin):
    route, number = name.rsplit('--', 1)
    number = int(number)
    source = self.find(name)
    if source is None or source.descriptor() != origin:
      return None
    sources = [source]
    if center - PADDING_NS < first and number > 0:
      previous = self.find(f'{route}--{number - 1}', wait_closed=True)
      if previous is WAITING_FOR_CLOSE:
        return WAITING_FOR_CLOSE
      if previous is not None:
        sources.insert(0, previous)
    if center + PADDING_NS > last:
      following = self.find(f'{route}--{number + 1}', wait_closed=True)
      if following is WAITING_FOR_CLOSE:
        return WAITING_FOR_CLOSE
      if following is not None:
        sources.append(following)
    return [source.descriptor() for source in sources]

  def load_plan(self, plan):
    sources = []
    for segment, name, fingerprint in plan:
      if not SEGMENT.fullmatch(segment) or '..' in segment or name not in RLOG_NAMES or len(fingerprint) != 4:
        raise ValueError('invalid_event_clip_plan')
      sources.append(Source(self.root / segment / name, tuple(fingerprint)))
    if not 1 <= len(sources) <= 3:
      raise ValueError('invalid_event_clip_plan')
    return sources

  def prepare(self, output, sources, name, center):
    self.sources_unchanged(sources)
    if output.is_file() and not output.is_symlink() and 0 < output.stat().st_size <= COPY_LIMIT:
      return
    import zstandard

    temporary = output.with_suffix('.part')
    temporary.unlink(missing_ok=True)
    context, kept = {}, 0
    try:
      with temporary.open('xb') as raw:
        os.chmod(temporary, 0o600)
        writer = BoundedWriter(raw, COPY_LIMIT, self.check)
        with zstandard.ZstdCompressor(level=1).stream_writer(writer, closefd=False) as target:
          for source in sources:
            for frame in self.read_frames(source):
              with self.schema.Event.from_bytes(frame, traversal_limit_in_words=2**21) as event:
                topic, stamp = event.which(), event.logMonoTime
                if topic in ('initData', 'carParams'):
                  if topic not in context or source.path.parent.name == name:
                    context[topic] = private_message(event, self.schema)
                  continue
                if topic not in DIAGNOSTICS or not center - PADDING_NS <= stamp <= center + PADDING_NS:
                  continue
                if topic in PRIVATE_FIELDS:
                  frame = private_message(event, self.schema)
              target.write(frame)
              kept += 1
          # Source metadata retains its original samples without extending the clip clock.
          for entry in context.values():
            target.write(entry)
        raw.flush()
        os.fsync(raw.fileno())
      self.sources_unchanged(sources)
      if not kept:
        raise ValueError('empty_event_clip')
      os.replace(temporary, output)
    finally:
      temporary.unlink(missing_ok=True)

  def forget(self, name, center, token):
    self.check()
    if token is not None:
      if re.fullmatch(r'[0-9a-f]{64}', token) is None:
        raise ValueError('invalid_event_clip_token')
      output = self.cache / (token + '.zst')
      output.unlink(missing_ok=True)
      output.with_suffix('.part').unlink(missing_ok=True)
    with self.source.db() as db:
      db.execute('DELETE FROM clips WHERE segment=? AND center=?', (name, center))

  def next(self, commit):
    waiting = set()
    while self.check():
      with self.source.db() as db:
        rows = db.execute('''SELECT segment,center,first,last,origin,plan,token FROM clips
          ORDER BY segment COLLATE BINARY DESC,center DESC''')
        row = next((row for row in rows if (row[0], row[1]) not in waiting), None)
      if row is None:
        raw = self.source.next(commit)
        if raw is None:
          return None
        name = raw['manifest']['segment']
        entry = raw['manifest']['files'][0]
        source = Source(self.root / name / entry['name'], raw['identities'][entry['file_id']])
        centers, first, last = self.scan(source)
        self.source.check()
        with self.source.db() as db:
          db.executemany('INSERT OR IGNORE INTO clips VALUES (?,?,?,?,?,NULL,NULL)',
                         ((name, center, first, last, json.dumps(source.descriptor())) for center in centers))
        self.source.completed(raw)
        continue

      name, center, first, last, origin, plan, token = row
      if plan is None:
        plan = self.new_plan(name, center, first, last, json.loads(origin))
        # A locked neighbor may still cover the missing end of this clip. Leave
        # it pending, let other clips progress, and retry on the next recorder roll.
        if plan is WAITING_FOR_CLOSE:
          waiting.add((name, center))
          continue
        if plan is None:
          self.forget(name, center, token)
          continue
        token = identifier(self.key, 'stationary-clip-v2', [name, center, plan])
        self.check()
        with self.source.db() as db:
          db.execute('UPDATE clips SET plan=?,token=? WHERE segment=? AND center=?',
                     (json.dumps(plan), token, name, center))
      else:
        plan = json.loads(plan)
        if identifier(self.key, 'stationary-clip-v2', [name, center, plan]) != token:
          raise ValueError('invalid_event_clip_token')
      sources = self.load_plan(plan)
      output = self.cache / (token + '.zst')
      try:
        self.prepare(output, sources, name, center)
      except (SourceChanged, FileNotFoundError, NotADirectoryError):
        self.forget(name, center, token)
        continue
      segment = 'event-' + token + '--0'
      output_identity = identity(output)
      entry = {'name': 'rlog.zst', 'size': output_identity[2], 'mtime_ns': center}
      entry['file_id'] = file_id(self.key, segment, entry['name'], entry['size'], center)
      value = {'version': 1, 'privacy': PRIVACY, 'segment': segment, 'uploader_commit': commit, 'files': [entry]}
      return {'manifest': value, 'output': output, 'output_identity': output_identity, 'sources': sources,
              'name': name, 'center': center, 'token': token}
    return None

  def read(self, candidate, entry, offset, size):
    self.sources_unchanged(candidate['sources'])
    path = candidate['output']
    if identity(path) != candidate['output_identity'] or not 0 <= offset < entry['size'] or not 0 < size <= CHUNK:
      raise SourceChanged('event_copy_changed')
    with path.open('rb') as source:
      source.seek(offset)
      data = source.read(min(size, entry['size'] - offset))
    self.sources_unchanged(candidate['sources'])
    if identity(path) != candidate['output_identity'] or len(data) != min(size, entry['size'] - offset):
      raise SourceChanged('event_copy_changed')
    return data

  def completed(self, candidate):
    self.sources_unchanged(candidate['sources'])
    self.forget(candidate['name'], candidate['center'], candidate['token'])
