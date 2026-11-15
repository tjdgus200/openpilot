import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import zstandard

from openpilot.system.pv5_incidents.archive_catalog import Catalog, SourceChanged
from openpilot.system.pv5_incidents.archive_protocol import PRIVACY, manifest
from openpilot.system.pv5_incidents.archive_sanitize import PrivacyCatalog, Sanitizer, excluded
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from tools.pv5_archive.analyze import Triage, analyze
from tools.pv5_archive.schema import full_schema


class PrivacyChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='pv5-privacy-test-')
    self.root = Path(self.temp.name)
    self.context = full_schema()
    self.schema = self.context.__enter__()
    self.allowed = True

  def tearDown(self):
    self.context.__exit__(None, None, None)
    self.temp.cleanup()

  def log(self, path):
    events = []
    init = self.schema.Event.new_message(logMonoTime=1000000000, valid=True)
    init.initData.gitCommit = 'b' * 40
    events.append(init.to_bytes())
    cp = self.schema.Event.new_message(logMonoTime=100000000000, valid=True)
    cp.init('carParams').carFingerprint = 'KIA_PV5'
    events.append(cp.to_bytes())
    for topic in ('rawAudioData', 'audioFeedback', 'driverCameraState', 'driverEncodeData', 'livestreamDriverEncodeData'):
      event = self.schema.Event.new_message(logMonoTime=100000000000, valid=True)
      event.init(topic)
      if topic == 'rawAudioData':
        event.rawAudioData.data = b'PRIVATE_MICROPHONE_PAYLOAD'
      events.append(event.to_bytes())
    dm = self.schema.Event.new_message(logMonoTime=100000000000, valid=True)
    dm.init('driverMonitoringState')
    events.append(dm.to_bytes())
    for index in range(50):
      event = self.schema.Event.new_message(logMonoTime=100000000000 + index * 20000000, valid=True)
      cs = event.init('carState')
      cs.canValid, cs.vEgo, cs.aEgo, cs.cruiseState.enabled = True, 12.0, (-4.0 if index >= 20 else 0.0), index < 35
      events.append(event.to_bytes())
    path.write_bytes(zstandard.ZstdCompressor().compress(b''.join(events)))
    return hashlib.sha256(path.read_bytes()).hexdigest()

  def test_full_schema_filter_exact_native_frames_original_unchanged_and_triage(self):
    source = self.root / 'rlog.zst'
    digest = self.log(source)
    sanitizer = Sanitizer(self.root / 'cache', lambda: True, self.schema)
    output, name, audit = sanitizer.prepare(source, 'a' * 64)
    self.assertEqual(name, 'rlog.zst')
    self.assertEqual(audit['filtered_messages'], 5)
    with decoded(output) as stream:
      topics = []
      for frame in messages(stream):
        with self.schema.Event.from_bytes(frame) as event:
          topics.append(event.which())
    self.assertIn('driverMonitoringState', topics)
    self.assertFalse(any(excluded(topic) for topic in topics))
    self.assertNotIn(b'PRIVATE_MICROPHONE_PAYLOAD', zstandard.ZstdDecompressor().decompress(output.read_bytes(), max_output_size=1024**2))
    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
    self.assertEqual(sanitizer.prepare(source, 'a' * 64)[0], output)
    result = analyze(output, self.schema)
    self.assertEqual(result['source_commit'], 'b' * 40)
    self.assertEqual(result['duration_seconds'], 0.98)  # Not initData's boot timestamp.
    self.assertEqual(result['topic_counts']['carState'], 50)
    self.assertEqual({item['kind'] for item in result['candidates']}, {'hard_deceleration', 'negative_jerk', 'cruise_release'})
    self.assertFalse(result['visual_semantics_evaluated'])
    with self.assertRaisesRegex(ValueError, 'received_excluded_media'):
      analyze(source, self.schema)

  def test_privacy_manifest_cache_resume_and_completed_cleanup(self):
    folder = self.root / 'logs' / '00000341--1810673eb4--14'
    folder.mkdir(parents=True)
    self.log(folder / 'rlog.zst')
    (folder / 'fcamera.hevc').write_bytes(b'elementary_video')
    (folder / 'dcamera.hevc').write_bytes(b'PRIVATE_DRIVER_CAMERA')
    for path in folder.iterdir():
      os.utime(path, (1000, 1000))
    raw = Catalog(folder.parent, self.root / 'catalog.sqlite', bytes(32), lambda: self.allowed, clock=lambda: 2000)
    cache = self.root / 'cache'
    safe = PrivacyCatalog(raw, Sanitizer(cache, raw.check, self.schema))
    candidate = safe.next('c' * 40)
    self.assertEqual(candidate['manifest']['privacy'], PRIVACY)
    manifest(bytes(32), candidate['manifest'])
    self.assertEqual({row['name'] for row in candidate['manifest']['files']}, {'rlog.zst', 'fcamera.hevc'})
    for entry in candidate['manifest']['files']:
      self.assertTrue(safe.read(candidate, entry, 0, 65536))
    self.allowed = False
    with self.assertRaises(SourceChanged):
      safe.read(candidate, candidate['manifest']['files'][0], 0, 1)
    self.allowed = True
    resumed = PrivacyCatalog(raw, Sanitizer(cache, raw.check, self.schema)).next('c' * 40)
    self.assertEqual(candidate['manifest'], resumed['manifest'])
    safe.completed(candidate)
    self.assertEqual(list(cache.iterdir()), [])
    self.assertIsNone(safe.next('c' * 40))
    self.assertTrue((folder / 'dcamera.hevc').exists())

  def test_truncation_and_admission_loss_are_not_success(self):
    source = self.root / 'rlog.zst'
    self.log(source)
    with self.assertRaises(SourceChanged):
      Sanitizer(self.root / 'cache', lambda: (_ for _ in ()).throw(SourceChanged()), self.schema).prepare(source, 'f' * 64)
    source.write_bytes(zstandard.ZstdCompressor().compress(b'\xff\xff\xff\xff'))
    with self.assertRaises(ValueError):
      Sanitizer(self.root / 'cache2', lambda: True, self.schema).prepare(source, 'f' * 64)

  def test_actual_schema_scc_command_and_ttc_are_not_measured_braking(self):
    triage = Triage()
    for topic in ('radarState', 'carOutput', 'carState'):
      event = self.schema.Event.new_message(logMonoTime=100000000000, valid=True)
      value = event.init(topic)
      if topic == 'radarState':
        value.leadOne.status, value.leadOne.dRel, value.leadOne.vRel = True, 8.0, -5.0
      elif topic == 'carOutput':
        value.actuatorsOutput.pv5SccDecelActive = True
        value.actuatorsOutput.pv5SccWarningRaw = -5.0
        value.actuatorsOutput.pv5SccWarningBaseAccel = 0.0
        value.actuatorsOutput.accel = -5.0
      else:
        value.canValid, value.vEgo, value.aEgo = True, 20.0, 0.0
      with self.schema.Event.from_bytes(event.to_bytes()) as parsed:
        triage.accept(parsed)
    result = triage.result()
    self.assertEqual({row['kind'] for row in result['candidates']}, {'short_recorded_ttc', 'scc_deceleration_assist'})
    self.assertEqual(result['min_accel'], 0.0)
    self.assertEqual(result['samples'][0]['command_accel'], -5.0)

  @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'native ffmpeg/ffprobe unavailable')
  def test_actual_aac_container_becomes_video_only_without_reencoding(self):
    ffmpeg, probe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    source = self.root / 'qcamera.ts'
    subprocess.run(
      [
        ffmpeg,
        '-nostdin',
        '-v',
        'error',
        '-f',
        'lavfi',
        '-i',
        'color=size=64x64:rate=10',
        '-f',
        'lavfi',
        '-i',
        'sine=frequency=440',
        '-t',
        '0.4',
        '-c:v',
        'libx264',
        '-c:a',
        'aac',
        str(source),
      ],
      check=True,
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    sanitizer = Sanitizer(self.root / 'cache', lambda: True, self.schema, ffmpeg, probe)
    self.assertIn('audio', sanitizer.streams(source))
    output, name, audit = sanitizer.prepare(source, 'e' * 64)
    self.assertEqual(name, 'qcamera.ts')
    self.assertTrue(audit['audio_removed'])
    self.assertEqual(sanitizer.streams(output), ['video'])

    def video_digest(path):
      result = subprocess.run(
        [ffmpeg, '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-c:v', 'copy', '-f', 'hash', '-hash', 'sha256', '-'],
        capture_output=True,
        check=True,
      )
      return result.stdout

    self.assertEqual(video_digest(source), video_digest(output))
    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
  unittest.main()
