"""Pinned DATA tools must remain protected and immutable before execution."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from openpilot.system.pv5_incidents.archive_sanitize import MEDIA_SOURCE, data_media_tool


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0, 'native root ownership fixture')
class MediaToolsChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.root.chmod(0o755)
    self.folder = self.root / ('ffmpeg-9.0.2-' + MEDIA_SOURCE[:16])
    (self.folder / 'bin').mkdir(parents=True)
    self.binary = self.folder / 'bin/ffprobe'
    data = b'\x7fELF' + bytes(14) + (183).to_bytes(2, 'little') + b'public tool fixture'
    self.binary.write_bytes(data)
    self.binary.chmod(0o755)
    self.value = {'source_sha256': MEDIA_SOURCE, 'version': '9.0.2', 'network_protocols_enabled': False,
                  'files': {'ffprobe': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}
    self.save()

  def save(self):
    (self.folder / 'manifest.json').write_text(json.dumps(self.value))
    (self.folder / 'manifest.json').chmod(0o644)

  def test_checked_tool_and_missing_optional_install(self):
    self.assertEqual(data_media_tool('ffprobe', lambda: True, self.root), str(self.binary))
    self.assertIsNone(data_media_tool('ffprobe', lambda: True, self.root / 'absent'))

  def test_writable_and_symlink_tools_are_refused(self):
    self.binary.chmod(0o777)
    with self.assertRaisesRegex(ValueError, 'unsafe_media_tool_binary'):
      data_media_tool('ffprobe', lambda: True, self.root)
    self.binary.chmod(0o755)
    target = self.binary.with_name('original')
    self.binary.rename(target)
    self.binary.symlink_to(target)
    with self.assertRaisesRegex(ValueError, 'unsafe_media_tool_binary'):
      data_media_tool('ffprobe', lambda: True, self.root)

  def test_changed_source_or_network_enabled_manifest_is_refused(self):
    for field, value in (('source_sha256', '0' * 64), ('network_protocols_enabled', True)):
      original = self.value[field]
      self.value[field] = value
      self.save()
      with self.assertRaisesRegex(ValueError, 'media_tool_source_identity'):
        data_media_tool('ffprobe', lambda: True, self.root)
      self.value[field] = original

  def test_binary_corruption_and_permission_loss_are_refused(self):
    data = self.binary.read_bytes()
    self.binary.write_bytes(data[:-1] + b'X')
    with self.assertRaisesRegex(ValueError, 'media_tool_binary_identity'):
      data_media_tool('ffprobe', lambda: True, self.root)
    self.binary.write_bytes(data)
    with self.assertRaisesRegex(ValueError, 'test_permission_lost'):
      data_media_tool('ffprobe', lambda: (_ for _ in ()).throw(ValueError('test_permission_lost')), self.root)


if __name__ == '__main__':
  unittest.main()
