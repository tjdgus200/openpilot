"""Exercise the production datagram and camera/radar context without a GPU."""
import os
from pathlib import Path
import json
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools/jetlink')]
from openpilot.common import pv5_radar_label_context as context
from hud_track_classes import RadarRows, TrackClassReader


@unittest.skipUnless(sys.platform.startswith('linux'), 'production Linux Unix datagrams')
class TransportTests(unittest.TestCase):
  def test_marker_is_discarded_before_existing_hud_content(self):
    from hud_protocol import encode_snapshot
    hud = {'cameras': {'road': 'a' * 97000}, 'events': {}, 'indicator_display': {'on': True}}
    expected = encode_snapshot(dict(hud))
    self.assertEqual(encode_snapshot(dict(hud, radar_labels={'points': [[1,2,3,4]] * 450})), expected)


  def test_real_marker_pairs_only_causal_measured_radar(self):
    origin = time.monotonic_ns()
    class SM(dict):
      updated = {'liveTracks': True}
      valid = {'liveTracks': True}
      logMonoTime = {'liveTracks': origin-20_000_000}
    def point(id, source, measured):
      return SimpleNamespace(trackId=id, radarSource=source, measured=measured, dRel=20., yRel=0., vRel=1.)
    radar = SimpleNamespace(points=[point(56, 'frontRadar', True), point(4, 'frontRadar', False),
                                   point(9, 'cornerRadar', True)], radarTrackFlipped=False,
                            errors=SimpleNamespace(to_dict=lambda: {}))
    sm = SM(liveTracks=radar)
    with patch.object(context, 'MARKER_ADDRESS', '\0pv5.test.labels.' + str(os.getpid())):
      collect = context.RadarContext()
      source = context.CaptureMarker()
      self.addCleanup(collect.sock.close)
      self.addCleanup(source.sock.close)
      source.geometry = origin-10_000_000, []
      pair = bytes(context.LENS_BYTES) + bytes([123]) * context.LENS_BYTES
      source.identify(9, pair)
      self.assertIsNone(collect.sample(sm))
      source.publish(9, origin)
      source.identify(10, pair)
      source.publish(10, origin+1)
      sm.logMonoTime['liveTracks'] = origin+10_000_000
      batch = collect.sample(sm)
      self.assertEqual([m['frame'] for m in batch], [9, 10])
      joined = batch[-1]
      self.assertEqual((joined['lens'], joined['fingerprint']), ('wide', '7b'*16))
      self.assertEqual(joined['radar_ns'], origin-20_000_000)
      self.assertEqual(joined['points'], [[56, 20., 0., 1.]])
      with patch('time.monotonic_ns', return_value=origin+300_000_000):
        self.assertIsNone(collect.sample(sm))

  def test_single_camera_cannot_tag_a_wide_frame(self):
    marker = context.CaptureMarker()
    self.addCleanup(marker.sock.close)
    marker.geometry = (123, [])
    marker.identify(1, bytes(context.LENS_BYTES))
    self.assertIsNone(marker.image)

  def test_renderer_rejects_reused_id_old_camera_or_expired_result(self):
    now = 2_000_000_000
    hud = dict(valid={'liveTracks': True}, alive={'liveTracks': True}, mono={'liveTracks': 765}, events={'liveTracks':'x'})
    row = dict(track_id=34, class_id=0, expires_monotonic_ns=now+20)
    pkt = dict(version=1, lens='wide', radar_ns=765, published_monotonic_ns=now, labels=[row])
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'labels.json'
      for edits, result in [({}, {34: 0}), ({'radar_ns': 764}, {}),
                            ({'lens': 'road'}, {}), ({'labels': [dict(row, expires_monotonic_ns=now)]}, {})]:
        path.write_text(json.dumps(dict(pkt, **edits)))
        self.assertEqual(TrackClassReader(path).read((2., hud), now), result)

      pkt['labels'] = [dict(row, point=[20., 0.2, -5.])]
      path.write_text(json.dumps(pkt))
      hud['mono']['liveTracks'] = 765 + 50_000_000
      for points, visible in [([(34, 19.75, .3, -5.)], True), ([(43, 19.75, .3, -5.)], False),
                              ([(34, 19.75, .3, -5.), (43, 20., .2, -5.)], False),
                              ([(34, 25., 2., -5.)], False)]:
        reader = TrackClassReader(path, SimpleNamespace(event=None, get=lambda _: points))
        self.assertEqual(bool(reader.read((2., hud), now)), visible)
      hud['mono']['liveTracks'] += 60_000_000
      self.assertFalse(reader.read((2., hud), now))

  def test_cache_tracks_atomic_replacement_radar_updates_and_per_label_expiry(self):
    now = 2_000_000_000
    hud = dict(valid={'liveTracks': True}, alive={'liveTracks': True}, mono={'liveTracks': 765}, events={'liveTracks':'x'})
    rows = [dict(track_id=k, class_id=0, expires_monotonic_ns=now+t, point=[20., 0., 0.])
            for k,t in ((34, 60_000_000), (43, 200_000_000))]
    pkt = dict(version=1, lens='wide', radar_ns=765, published_monotonic_ns=now, labels=rows)
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'labels.json'
      path.write_text(json.dumps(pkt))
      reader = TrackClassReader(path)
      with patch('hud_track_classes.json.loads', wraps=json.loads) as loads:
        expected = reader.read((2., hud), now)
        self.assertEqual(set(expected), {34, 43})
        for dt in (1, 10_000_000, 53_000_000):
          self.assertIs(reader.read((2., hud), now+dt), expected)
        self.assertEqual(set(reader.read((2., hud), now+60_000_000)), {43})
        self.assertEqual(loads.call_count, 1)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(pkt, labels=[])))
        tmp.replace(path)
        self.assertFalse(reader.read((2., hud), now+100_000_000))
        self.assertEqual(loads.call_count, 2)

  def test_radar_event_already_decoded_by_hud_is_reused(self):
    radar_rows = RadarRows()
    hud = dict(mono={'liveTracks': 23}, events={'liveTracks':'x'})
    point = SimpleNamespace(measured=True, radarSource='frontRadar', trackId=34, dRel=12., yRel=0., vRel=-1.)
    radar = SimpleNamespace(points=[point], radarTrackFlipped=False, errors=SimpleNamespace(to_dict=lambda: {}))
    radar_rows.observe(SimpleNamespace(logMonoTime=23, valid=True, liveTracks=radar))
    rows = radar_rows.get(hud)
    self.assertEqual(rows, ((34, 12., 0., -1.),))
    self.assertIs(radar_rows.get(hud), rows)
    hud['mono']['liveTracks'] = 24
    self.assertEqual(radar_rows.get(hud), ())

  def test_sixty_hz_draws_parse_twenty_hz_atomic_publications_once(self):
    hud = dict(valid={'liveTracks': True}, alive={'liveTracks': True}, mono={'liveTracks': 765}, events={'liveTracks':'x'})
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'labels.json'
      reader = TrackClassReader(path)
      with patch('hud_track_classes.json.loads', wraps=json.loads) as loads:
        for i in range(600):
          now = 2_000_000_000 + i * 1_000_000_000 // 60
          if i % 3 == 0:
            packet = dict(version=1, lens='wide', radar_ns=765, published_monotonic_ns=now,
                          labels=[dict(track_id=34, class_id=2, expires_monotonic_ns=now+250_000_000)])
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(packet))
            temp.replace(path)
          self.assertEqual(reader.read((now/1e9, hud), now), {34: 2})
        self.assertEqual(loads.call_count, 200)


if __name__ == '__main__':
  unittest.main()
