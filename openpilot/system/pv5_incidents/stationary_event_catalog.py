"""Token-free screening of existing closed recordings, only under stop permission.

No live subscriber, control output or recording changes. The ordinary home
catalog remains independent of the scan cursor; completed transfer IDs are shared.
"""
import math
import os
import stat

from openpilot.system.pv5_incidents.archive_catalog import Catalog, SourceChanged
from openpilot.system.pv5_incidents.archive_protocol import MAX_FILE, RLOG_NAMES, SEGMENT, file_id
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from openpilot.system.pv5_incidents.archive_sanitize import identity
from openpilot.system.pv5_incidents.priority import REPORTABLE_ALERTS


class Evidence:
  """Selection heuristics only; strong OEM request is not proof of actual braking."""
  def __init__(self):
    self.car_time = None
    self.hard_since = None
    self.last_car = None

  def observe(self, event):
    if not event.valid:
      if event.which() == 'carState':
        self.car_time = self.hard_since = self.last_car = None
      return None
    now = event.logMonoTime * 1e-9
    if not math.isfinite(now) or now <= 0:
      return None
    topic = event.which()
    if topic == 'carState':
      cs = event.carState
      if not cs.canValid or cs.canTimeout or not all(math.isfinite(v) for v in (cs.vEgo, cs.aEgo)):
        self.car_time = self.hard_since = self.last_car = None
        return None
      self.car_time = now
      if cs.stockAeb:
        return 'recorded_stock_aeb'
      if 2 <= cs.vEgo <= 80 and -20 <= cs.aEgo <= -3:
        if self.last_car is None or not 0 < now - self.last_car <= .3:
          self.hard_since = now
        if self.hard_since is None:
          self.hard_since = now
        self.last_car = now
        if now - self.hard_since >= .2:
          return 'hard_deceleration'
      else:
        self.hard_since = None
        self.last_car = now
    elif topic == 'carOutput':
      output = event.carOutput.actuatorsOutput
      stamp = output.pv5SccWarningMonoTime
      if (self.car_time is not None and 0 <= now - self.car_time <= .3
          and output.pv5SccWarningVersion == 1 and stamp > 0
          and stamp == output.pv5SccDecelSourceMonoTime and 0 <= event.logMonoTime - stamp <= 150_000_000
          and math.isfinite(output.pv5SccWarningRaw) and -4 <= output.pv5SccWarningRaw <= -3):
        return 'stock_scc_hard_request'
    elif topic == 'onroadEvents':
      if any(str(e.name) in REPORTABLE_ALERTS and (e.immediateDisable or e.softDisable or e.permanent) for e in event.onroadEvents):
        return 'recorded_system_fault'
    return None


def screen(catalog, candidate, check):
  from openpilot.cereal import log
  entry = candidate['manifest']['files'][0]
  path = catalog.root / candidate['manifest']['segment'] / entry['name']
  before = identity(path)
  if before != candidate['identities'][entry['file_id']]:
    raise SourceChanged('event_source_changed')

  def guarded():
    check()
    catalog.check()
    if identity(path) != before or path.parent.is_symlink() or catalog.locked(path.parent):
      raise SourceChanged('event_source_changed')
    return True

  evidence = Evidence()
  guarded()
  with decoded(path) as stream:
    for frame in messages(stream, guarded):
      with log.Event.from_bytes(frame, traversal_limit_in_words=2**21) as event:
        reason = evidence.observe(event)
      if reason:
        guarded()
        return reason
  guarded()
  return None


class EventCatalog(Catalog):
  def __init__(self, root, database, key, allowed, **kwargs):
    super().__init__(root, database, key, allowed, **kwargs)
    self.scan = RecentLogCatalog(root, self.database.with_name('archive-event-scan.sqlite'), key, allowed, groups=(RLOG_NAMES,), **kwargs)
    self.budget_check = self.check
    with self.scan.db() as db:
      db.execute('CREATE TABLE IF NOT EXISTS selected(segment TEXT PRIMARY KEY, reason TEXT NOT NULL)')

  def next(self, uploader_commit):
    while True:
      self.budget_check()
      with self.scan.db() as db:
        wanted = {row[0] for row in db.execute('SELECT segment FROM selected')}
      candidate = selected_next(self, uploader_commit, wanted=wanted, require_media=True)
      if candidate is not None:
        return candidate
      raw = self.scan.next(uploader_commit)
      if raw is None:
        return None
      reason = screen(self.scan, raw, self.budget_check)
      self.check()
      if reason:
        route, segment = raw['manifest']['segment'].rsplit('--', 1)
        # Whole existing adjacent segments retain event-boundary context. PC AI
        # still reviews only risk-first selected windows, not whole recordings.
        names = [f'{route}--{n}' for n in range(max(0, int(segment) - 1), int(segment) + 2)]
        with self.scan.db() as db:
          if db.execute('SELECT count(*) FROM selected').fetchone()[0] >= 4096:
            stale = [row[0] for row in db.execute('SELECT segment FROM selected') if not (self.root / row[0]).exists()]
            db.executemany('DELETE FROM selected WHERE segment=?', [(name,) for name in stale])
            if db.execute('SELECT count(*) FROM selected').fetchone()[0] >= 4096:
              raise ValueError('event_selection_budget')
          db.executemany('INSERT OR IGNORE INTO selected VALUES (?,?)', [(name, reason) for name in names])
      self.scan.completed(raw)


def selected_next(self, uploader_commit, wanted=None, reverse=False, require_media=False):
  self.check()
  # Fixed bounded metadata scan, only while admitted. Finished immutable segments
  # hit one directory stat/cache lookup; their files are not rescanned.
  names = []
  with os.scandir(self.root) as entries:
    for index, entry in enumerate(entries):
      self.check()
      if index >= 4096:
        raise ValueError('archive_directory_budget')
      if (wanted is None or entry.name in wanted) and SEGMENT.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
        names.append(entry.name)
  for name in sorted(names, key=lambda name: (name.rsplit('--', 1)[0], int(name.rsplit('--', 1)[1])), reverse=reverse):
    self.check()
    folder = self.root / name
    try:
      info = folder.stat(follow_symlinks=False)
      if not stat.S_ISDIR(info.st_mode):
        continue
      with self.db() as db:
        done = db.execute('SELECT inode,mtime FROM completed WHERE segment=?', (name,)).fetchone()
      if done == (info.st_ino, info.st_mtime_ns):
        continue
      if self.locked(folder):
        continue
      files, identities = [], {}
      for group in self.groups:
        for filename in group:
          path = folder / filename
          try:
            meta = path.stat(follow_symlinks=False)
          except FileNotFoundError:
            continue
          if not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= MAX_FILE:
            raise ValueError('unsafe_or_oversized_source')
          if not 2 <= self.clock() - meta.st_mtime < 10**9:
            raise SourceChanged('source_not_closed')
          token = file_id(self.key, name, filename, meta.st_size, meta.st_mtime_ns)
          files.append({'name': filename, 'size': meta.st_size, 'mtime_ns': meta.st_mtime_ns, 'file_id': token})
          identities[token] = (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns)
          break
      if not files or (require_media and (not any(f['name'] in RLOG_NAMES for f in files) or not any(not f['name'].startswith('rlog') for f in files))):
        continue
      return {
        'manifest': {'version': 1, 'segment': name, 'uploader_commit': uploader_commit, 'files': files},
        'identities': identities,
        'directory_identity': (info.st_ino, info.st_mtime_ns),
      }
    except (FileNotFoundError, NotADirectoryError):
      continue  # Existing deleter always wins; no locks or preservation hooks.
  return None


class RecentLogCatalog(Catalog):
  def next(self, uploader_commit):
    return selected_next(self, uploader_commit, reverse=True)
