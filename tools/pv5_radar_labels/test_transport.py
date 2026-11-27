"""Exercise the production datagram and camera/radar context without a GPU."""
import os
from collections import deque
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
from hud_protocol import HUD_SNAPSHOT_LIMIT, encode_snapshot
from hud_track_classes import RadarRows, TrackClassReader


class SnapshotBudgetTests(unittest.TestCase):
  @staticmethod
  def wire(record):
    return json.dumps(record, separators=(',', ':')).encode()

  @staticmethod
  def marker(frame, point_count=128):
    return dict(frame=frame, capture_ns=1_000_000_000 + frame, calibration_ns=900_000_000,
                lens='wide', projection=[[1., 0., 0., 0.]] * 3, fingerprint='ab' * 16,
                radar_ns=990_000_000, points=[[i, 1.23456789, -2.3456789, 3.456789] for i in range(point_count)])

  def test_overflow_keeps_latest_complete_markers_at_exact_boundary(self):
    markers = tuple(self.marker(frame) for frame in range(6))
    hud = {'cameras': {'road': ''}, 'events': {'liveTracks': 'radar'},
           'indicator_display': {'on': True}, 'nav_map_preview': '지도"\\'}
    hud['cameras']['road'] = 'a' * (HUD_SNAPSHOT_LIMIT - len(self.wire(dict(hud, radar_labels=markers[-2:]))))
    for extra, retained in ((0, 2), (1, 1)):
      with self.subTest(extra=extra):
        candidate = {'radar_labels': markers, **hud, 'cameras': {'road': hud['cameras']['road'] + 'a' * extra}}
        base = {key: value for key, value in candidate.items() if key != 'radar_labels'}
        with patch('hud_protocol.json.dumps', wraps=json.dumps) as dumps:
          actual = encode_snapshot(candidate)
          self.assertEqual(sum(call.args[0] is candidate for call in dumps.call_args_list), 2)
        self.assertEqual(actual, self.wire(dict(base, radar_labels=markers[-retained:])))
        self.assertLessEqual(len(actual), HUD_SNAPSHOT_LIMIT)
        self.assertEqual(candidate, dict(base, radar_labels=markers[-retained:]))
    self.assertEqual(len(self.wire(dict(hud, radar_labels=markers[-2:]))), HUD_SNAPSHOT_LIMIT)

  def test_no_marker_fits_and_older_marker_cannot_skip_newest(self):
    markers = [self.marker(8, 0), self.marker(9)]
    hud = {'cameras': {'road': ''}, 'events': {}, 'indicator_display': {'on': True}}
    hud['cameras']['road'] = 'a' * (HUD_SNAPSHOT_LIMIT - len(self.wire(dict(hud, radar_labels=markers[:1]))))
    for batch in (markers, {'points': [[1, 2, 3, 4]] * 450}):
      with self.subTest(batch_type=type(batch)):
        candidate = dict(hud, radar_labels=batch)
        self.assertEqual(encode_snapshot(candidate), self.wire(hud))
        self.assertEqual(candidate, hud)

  def test_existing_core_pruning_can_make_space_without_changing_other_fields(self):
    hud = {'indicator_display': {'on': True}, 'signal_display': 's' * HUD_SNAPSHOT_LIMIT,
           'events': {'carState': 'essential'}, 'cameras': {'road': 'camera'}, 'nav_map_preview': 'map'}
    markers = [self.marker(4), self.marker(5)]
    base = json.loads(encode_snapshot(dict(hud)))
    self.assertEqual(base, {'events': hud['events'], 'cameras': hud['cameras'], 'nav_map_preview': 'map'})
    self.assertEqual(encode_snapshot(dict(hud, radar_labels=markers)), self.wire(dict(base, radar_labels=markers)))

  def test_under_limit_encodes_once_and_retains_atypical_wire_values(self):
    candidate = {'radar_labels': {'points': []}, 'events': {}, 0: 'numeric key'}
    with patch('hud_protocol.json.dumps', wraps=json.dumps) as dumps:
      actual = encode_snapshot(candidate)
      dumps.assert_called_once_with(candidate, separators=(',', ':'))
    self.assertEqual(actual, self.wire(candidate))

  def test_overflow_with_empty_base_produces_valid_json(self):
    huge = dict(self.marker(3), fingerprint='b' * HUD_SNAPSHOT_LIMIT)
    latest = self.marker(4)
    self.assertEqual(encode_snapshot({'radar_labels': [huge, latest]}), self.wire({'radar_labels': [latest]}))


@unittest.skipUnless(sys.platform.startswith('linux'), 'production Linux Unix datagrams')
class TransportTests(unittest.TestCase):
  def test_real_marker_pairs_only_causal_measured_radar(self):
    origin = time.monotonic_ns()
    def point(id, source, measured):
      return SimpleNamespace(trackId=id, radarSource=source, measured=measured, dRel=20., yRel=0., vRel=1.)
    radar = SimpleNamespace(points=[point(56, 'frontRadar', True), point(4, 'frontRadar', False),
                                   point(9, 'cornerRadar', True)], radarTrackFlipped=False,
                            errors=SimpleNamespace(to_dict=lambda: {}))
    events = deque([SimpleNamespace(which=lambda: 'liveTracks', valid=True, liveTracks=radar, logMonoTime=origin-20_000_000)])
    with patch.object(context, 'MARKER_ADDRESS', '\0pv5.test.labels.' + str(os.getpid())):
      with patch.object(context.time, 'monotonic_ns', return_value=origin-25_000_000):
        collect = context.RadarContext(lambda: events.popleft() if events else None)
      source = context.CaptureMarker()
      self.addCleanup(collect.close)
      self.addCleanup(source.sock.close)
      source.geometry = origin-10_000_000, []
      pair = bytes(context.LENS_BYTES) + bytes([123]) * context.LENS_BYTES
      source.identify(9, pair)
      self.assertIsNone(collect.sample())
      source.publish(9, origin)
      source.identify(10, pair)
      source.publish(10, origin+1)
      events.append(SimpleNamespace(which=lambda: 'liveTracks', valid=True, liveTracks=radar, logMonoTime=origin+10_000_000))
      with patch('time.monotonic_ns', return_value=origin+11_000_000):
        batch = collect.sample()
      self.assertEqual([m['frame'] for m in batch], [9, 10])
      joined = batch[-1]
      self.assertEqual((joined['lens'], joined['fingerprint']), ('wide', '7b'*16))
      self.assertEqual(joined['radar_ns'], origin-20_000_000)
      self.assertEqual(joined['points'], [[56, 20., 0., 1.]])
      with patch('time.monotonic_ns', return_value=origin+300_000_000):
        self.assertIsNone(collect.sample())

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
