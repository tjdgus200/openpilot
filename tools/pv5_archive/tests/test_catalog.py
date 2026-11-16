import os
from pathlib import Path
import tempfile
import unittest

from openpilot.system.pv5_incidents.archive_catalog import Catalog, SourceChanged


class CatalogChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='pv5-archive-catalog-test-')
    self.root = Path(self.temp.name)
    self.logs = self.root / 'logs'
    self.logs.mkdir()
    self.folder = self.logs / '00000341--1810673eb4--14'
    self.folder.mkdir()
    self.allowed = True
    for name in ('rlog.zst', 'fcamera.hevc', 'ecamera.hevc', 'qcamera.ts', 'dcamera.hevc', 'audio.wav'):
      path = self.folder / name
      path.write_bytes(name.encode() * 3)
      os.utime(path, (10, 10))
    self.catalog = Catalog(self.logs, self.root / 'state/catalog.sqlite', bytes(range(32)), lambda: self.allowed, clock=lambda: 100)

  def tearDown(self):
    self.temp.cleanup()

  def test_only_closed_original_road_files_and_unchanged_done_cache(self):
    candidate = self.catalog.next('a' * 40)
    self.assertEqual([entry['name'] for entry in candidate['manifest']['files']], ['rlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc'])
    for entry in candidate['manifest']['files']:
      self.assertEqual(self.catalog.read(candidate, entry, 0, 65536), (self.folder / entry['name']).read_bytes())
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in self.folder.iterdir()}
    self.catalog.completed(candidate)
    self.assertIsNone(self.catalog.next('a' * 40))
    self.assertEqual(before, {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in self.folder.iterdir()})

  def test_home_log_cursor_does_not_complete_or_hide_full_road_archive(self):
    from openpilot.system.pv5_incidents.archive_protocol import RLOG_NAMES
    logs = Catalog(self.logs, self.root / 'logs-only.sqlite', bytes(range(32)), lambda: self.allowed, clock=lambda:100, groups=(RLOG_NAMES,))
    candidate = logs.next('a'*40)
    self.assertEqual([e['name'] for e in candidate['manifest']['files']], ['rlog.zst'])
    logs.completed(candidate)
    self.assertIsNone(logs.next('a'*40))
    self.assertEqual(len(self.catalog.next('a'*40)['manifest']['files']),4)

  def test_each_logger_or_encoder_lock_blocks_the_segment(self):
    for name in ('rlog.lock', 'fcamera.hevc.lock', 'ecamera.hevc.lock', 'qcamera.ts.lock'):
      path = self.folder / name
      path.touch()
      self.assertIsNone(self.catalog.next('a' * 40))
      path.unlink()

  def test_movement_closes_source_reads_and_checkpoint(self):
    candidate = self.catalog.next('a' * 40)
    self.allowed = False
    with self.assertRaises(SourceChanged):
      self.catalog.read(candidate, candidate['manifest']['files'][0], 0, 65536)
    with self.assertRaises(SourceChanged):
      self.catalog.completed(candidate)

  def test_changed_reopened_deleted_or_symlink_sources_fail(self):
    candidate = self.catalog.next('a' * 40)
    entry = candidate['manifest']['files'][0]
    path = self.folder / entry['name']
    path.write_bytes(b'changed')
    with self.assertRaises(SourceChanged):
      self.catalog.read(candidate, entry, 0, 65536)
    path.unlink()
    with self.assertRaises(FileNotFoundError):
      self.catalog.read(candidate, entry, 0, 65536)
    outside = self.root / 'outside'
    outside.write_bytes(b'private')
    path.symlink_to(outside)
    with self.assertRaises(OSError):
      self.catalog.read(candidate, entry, 0, 65536)

  def test_missing_rlog_preserves_video_with_explicit_manifest(self):
    (self.folder / 'rlog.zst').unlink()
    candidate = self.catalog.next('a' * 40)
    self.assertEqual([entry['name'] for entry in candidate['manifest']['files']], ['qcamera.ts', 'fcamera.hevc', 'ecamera.hevc'])

  def test_segment_symlinks_and_unrelated_files_are_not_followed(self):
    other = self.logs / '00000341--1810673eb4--15'
    other.symlink_to(self.folder, target_is_directory=True)
    self.catalog.completed(self.catalog.next('a' * 40))
    self.assertIsNone(self.catalog.next('a' * 40))


if __name__ == '__main__':
  unittest.main()
