"""Stream only existing, closed road recordings. Never changes logger/deleter files."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import time

from openpilot.system.pv5_incidents.archive_protocol import MAX_FILE, RLOG_NAMES, SEGMENT, file_id

GROUPS = (RLOG_NAMES, ('qcamera.ts', 'qcamera.mp4'), ('fcamera.hevc', 'fcamera.ts'), ('ecamera.hevc', 'ecamera.ts'))


class SourceChanged(RuntimeError):
  pass


class Catalog:
  def __init__(self, root, database, key, allowed=lambda: False, clock=time.time, groups=GROUPS):
    self.root, self.database, self.key = Path(root).resolve(), Path(database), key
    self.allowed, self.clock = allowed, clock
    if groups not in (GROUPS, (RLOG_NAMES,)):
      raise ValueError("invalid_catalog_file_groups")
    self.groups = groups
    if self.database.is_symlink():
      raise ValueError('unsafe_archive_catalog')
    self.database.parent.mkdir(parents=True, exist_ok=True)
    with self.db() as db:
      db.execute('CREATE TABLE IF NOT EXISTS completed(segment TEXT PRIMARY KEY, inode INTEGER, mtime INTEGER)')

  @contextmanager
  def db(self):
    if self.database.exists() and self.database.stat().st_size > 2 * 1024 * 1024:
      raise ValueError('archive_catalog_budget')
    db = sqlite3.connect(self.database, timeout=0.2)
    try:
      db.execute('PRAGMA journal_mode=DELETE')
      db.execute('PRAGMA max_page_count=512')
      with db:
        yield db
    finally:
      db.close()

  def check(self):
    if self.allowed() is not True:
      raise SourceChanged('admission_lost')

  def locked(self, folder):
    with os.scandir(folder) as entries:
      for index, entry in enumerate(entries):
        self.check()
        if index >= 32:
          raise ValueError('archive_file_count_budget')
        if entry.name.endswith('.lock'):
          return True
    return False

  def next(self, uploader_commit):
    self.check()
    # Fixed bounded metadata scan, only while admitted. Finished immutable segments
    # hit one directory stat/cache lookup; their files are not rescanned.
    names = []
    with os.scandir(self.root) as entries:
      for index, entry in enumerate(entries):
        self.check()
        if index >= 4096:
          raise ValueError('archive_directory_budget')
        if SEGMENT.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
          names.append(entry.name)
    for name in sorted(names):
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
        if not files:
          continue
        return {
          'manifest': {'version': 1, 'segment': name, 'uploader_commit': uploader_commit, 'files': files},
          'identities': identities,
          'directory_identity': (info.st_ino, info.st_mtime_ns),
        }
      except (FileNotFoundError, NotADirectoryError):
        continue  # Existing deleter always wins; no locks or preservation hooks.
    return None

  def read(self, candidate, entry, offset, size):
    self.check()
    folder = self.root / candidate['manifest']['segment']
    if folder.is_symlink():
      raise SourceChanged('source_path_changed')
    if self.locked(folder):
      raise SourceChanged('source_reopened')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    directory = os.open(folder, flags | os.O_DIRECTORY)
    try:
      fd = os.open(entry['name'], flags, dir_fd=directory)
    finally:
      os.close(directory)
    try:
      expected = candidate['identities'][entry['file_id']]
      before = os.fstat(fd)
      if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != expected:
        raise SourceChanged('source_changed')
      if not 0 <= offset < entry['size'] or not 0 < size <= 65536:
        raise ValueError('invalid_source_read')
      os.lseek(fd, offset, os.SEEK_SET)
      data = os.read(fd, min(size, entry['size'] - offset))
      after = os.fstat(fd)
      if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != expected or len(data) != min(size, entry['size'] - offset):
        raise SourceChanged('source_changed')
      self.check()
      return data
    finally:
      os.close(fd)

  def completed(self, candidate):
    self.check()
    name = candidate['manifest']['segment']
    inode, mtime = candidate['directory_identity']
    info = (self.root / name).stat(follow_symlinks=False)
    if (info.st_ino, info.st_mtime_ns) != (inode, mtime):
      raise SourceChanged('source_directory_changed')
    with self.db() as db:
      if db.execute('SELECT count(*) FROM completed').fetchone()[0] >= 4096:
        # Drop only metadata for source directories already removed by the normal
        # deleter. Never delete, lock or pin source recordings.
        stale = [row[0] for row in db.execute('SELECT segment FROM completed') if not (self.root / row[0]).exists()]
        db.executemany('DELETE FROM completed WHERE segment=?', ((value,) for value in stale))
        if db.execute('SELECT count(*) FROM completed').fetchone()[0] >= 4096:
          raise ValueError('archive_catalog_budget')
      db.execute('INSERT OR REPLACE INTO completed VALUES(?,?,?)', (name, inode, mtime))
