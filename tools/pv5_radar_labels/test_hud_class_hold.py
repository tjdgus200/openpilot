"""Exercise radar-only HUD class continuity with real reader and radar events."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'jetlink'))
from hud_track_classes import RadarRows, TrackClassReader

MS = 1_000_000
JETSON_NS = 40_000 * MS
COMMA_NS = 10_000 * MS
ID = 56
CLASS = 2


def front(ident=ID, d=20., y=0., v=2., *, measured=True, source='frontRadar'):
  return NS(trackId=ident, dRel=d, yRel=y, vRel=v, measured=measured, radarSource=source)


class HudClassHoldTest(unittest.TestCase):
  def setUp(self):
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    self.path = Path(directory.name) / 'labels.json'
    self.radar = RadarRows()
    self.reader = TrackClassReader(self.path, self.radar)

  def publish(self, age_ms, labels, **fields):
    radar_ns = COMMA_NS + age_ms * MS
    now = JETSON_NS + age_ms * MS
    packet = dict(version=1, lens='wide', frame=100 + age_ms // 50,
                  capture_ns=radar_ns + 10 * MS, radar_ns=radar_ns,
                  published_monotonic_ns=now, labels=labels)
    packet.update(fields)
    temporary = self.path.with_suffix('.tmp')
    temporary.write_text(json.dumps(packet), encoding='utf-8')
    temporary.replace(self.path)

  def observe(self, age_ms, rows, *, radar_ms=None, valid=True, alive=True, present=True, flipped=False, errors=None):
    radar_ms = age_ms if radar_ms is None else radar_ms
    stamp = COMMA_NS + radar_ms * MS
    radar = NS(points=rows, radarTrackFlipped=flipped, errors=NS(to_dict=lambda: errors or {}))
    self.radar.observe(NS(logMonoTime=stamp, valid=valid, liveTracks=radar) if present else None)
    snapshot = dict(sent=(COMMA_NS + (age_ms + 13) * MS) / 1e9,
                    received={'liveTracks': (stamp + 5 * MS) / 1e9},
                    mono={'liveTracks': stamp}, events={'liveTracks': 'event'} if present else {},
                    valid={'liveTracks': valid}, alive={'liveTracks': alive})
    now = JETSON_NS + age_ms * MS
    return self.reader.read(((now - 2 * MS) / 1e9, snapshot), now)

  def seed(self, confidence=.8):
    self.publish(0, [dict(track_id=ID, class_id=CLASS, confidence=confidence,
                         point=[20., 0., 2.], expires_monotonic_ns=JETSON_NS + 290 * MS)])
    return self.observe(0, [front()])

  def test_explicit_empty_and_missing_camera_updates_keep_moving_radar_label(self):
    self.assertEqual(self.seed(), {ID: CLASS})
    self.publish(100, [])
    for age_ms in range(100, 1100, 100):
      with self.subTest(age_ms=age_ms):
        self.assertEqual(self.observe(age_ms, [front(d=20. + 2. * age_ms / 1000)]), {ID: CLASS})

  def test_radar_only_updates_hold_after_initial_camera_packet_expires(self):
    self.assertEqual(self.seed(), {ID: CLASS})
    for age_ms in range(100, 700, 100):
      with self.subTest(age_ms=age_ms):
        self.assertEqual(self.observe(age_ms, [front(d=20. + 2. * age_ms / 1000)]), {ID: CLASS})

  def test_camera_pairing_timeout_keeps_continuously_measured_radar_label(self):
    self.assertEqual(self.seed(), {ID: CLASS})
    self.publish(100, [], radar_ns=0, capture_ns=0)
    self.assertEqual(self.observe(100, [front(d=20.2)]), {ID: CLASS})
    self.assertEqual(self.observe(200, [front(d=20.4)]), {ID: CLASS})

  def test_same_id_return_before_stale_camera_evidence_expires_is_not_relabeled(self):
    self.assertEqual(self.seed(), {ID: CLASS})
    self.assertEqual(self.observe(50, [front(ident=ID + 1, d=20.1)]), {})
    self.assertEqual(self.observe(100, [front(d=20.2)]), {})
    self.publish(150, [dict(track_id=ID, class_id=CLASS, confidence=.9,
                            point=[20.3, 0., 2.], expires_monotonic_ns=JETSON_NS + 440 * MS)])
    self.assertEqual(self.observe(150, [front(d=20.3)]), {ID: CLASS})

  def test_lost_or_ambiguous_track_requires_new_camera_evidence(self):
    cases = {
      'no measured track': [],
      'merely predicted': [front(measured=False)],
      'same ID from another radar': [front(source='cornerRadar')],
      'nearby new ID': [front(ident=ID + 1, d=20.2)],
      'duplicate ID including far row': [front(d=20.2), front(d=45.)],
      'crossing competitor': [front(d=20.2), front(ident=ID + 1, d=20.2, y=.2)],
      'same ID with sudden range jump': [front(d=35.)],
      'same ID with sudden lateral jump': [front(d=20.2, y=3.)],
      'same ID with sudden speed jump': [front(d=20.2, v=20.)],
      'invalid position': [front(d=float('nan'))],
    }
    for reason, rows in cases.items():
      with self.subTest(reason=reason):
        self.reader = TrackClassReader(self.path, self.radar)
        self.assertEqual(self.seed(), {ID: CLASS})
        self.publish(100, [])
        self.assertEqual(self.observe(100, rows), {})
        self.assertEqual(self.observe(200, [front(d=20.4)]), {})

  def test_invalid_missing_or_discontinuous_radar_releases_hold(self):
    cases = {
      'invalid': (100, {'valid': False}),
      'not alive': (100, {'alive': False}),
      'missing': (100, {'present': False}),
      'flipped': (100, {'flipped': True}),
      'radar error': (100, {'errors': {'canError': True}}),
      'clock reversal': (100, {'radar_ms': -100}),
      'fresh HUD carrying an old radar event': (400, {'radar_ms': 0}),
      'new radar after a gap': (500, {}),
    }
    for reason, (age_ms, fields) in cases.items():
      with self.subTest(reason=reason):
        self.reader = TrackClassReader(self.path, self.radar)
        self.assertEqual(self.seed(), {ID: CLASS})
        self.publish(100, [])
        self.assertEqual(self.observe(age_ms, [front(d=20.2)], **fields), {})
        self.assertEqual(self.observe(age_ms + 100, [front(d=20.4)]), {})

  def test_low_confidence_can_show_live_but_cannot_seed_radar_hold(self):
    self.assertEqual(self.seed(.79), {ID: CLASS})
    self.publish(100, [])
    self.assertEqual(self.observe(100, [front(d=20.2)]), {})

  def test_malformed_optional_seed_is_not_retained(self):
    for edits in ({'confidence': None}, {'confidence': '0.95'}, {'point': None},
                  {'point': [20., 0.]}, {'point': ['20', 0., 2.]}):
      with self.subTest(edits=edits):
        self.reader = TrackClassReader(self.path, self.radar)
        row = dict(track_id=ID, class_id=CLASS, confidence=.90,
                   point=[20., 0., 2.], expires_monotonic_ns=JETSON_NS + 290 * MS)
        self.publish(0, [dict(row, **edits)])
        self.observe(0, [front()])
        self.publish(100, [])
        self.assertEqual(self.observe(100, [front(d=20.2)]), {})

  def test_reset_clears_hold(self):
    self.assertEqual(self.seed(), {ID: CLASS})
    self.publish(100, [])
    self.assertEqual(self.observe(100, [front(d=20.2)]), {ID: CLASS})
    self.reader.reset()
    self.assertEqual(self.observe(200, [front(d=20.4)]), {})


if __name__ == '__main__':
  unittest.main()
