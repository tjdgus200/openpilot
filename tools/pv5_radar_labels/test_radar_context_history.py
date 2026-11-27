"""Check causal radar pairing at the HUD's default 10 Hz publication cadence."""
from collections import deque
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from openpilot.common import pv5_radar_label_context as context


MS = 1_000_000
BASE_NS = 1_000 * MS


class RadarContextHistoryTest(unittest.TestCase):
  def setUp(self):
    self.radar_events = deque()
    self.marker_events = deque()
    self.radar_reads = 0
    self.context = self.open_context(0)
    self.addCleanup(lambda: self.context.close())

  def open_context(self, age_ms):
    private_socket = Mock()
    private_socket.recv.side_effect = self.recv_marker
    with (patch.object(context.socket, 'AF_UNIX', 1, create=True),
          patch.object(context.socket, 'socket', return_value=private_socket),
          patch.object(context.time, 'monotonic_ns', return_value=BASE_NS + age_ms * MS)):
      return context.RadarContext(self.recv_radar)

  def recv_radar(self):
    self.radar_reads += 1
    return self.radar_events.popleft() if self.radar_events else None

  def recv_marker(self, _):
    if self.marker_events:
      return json.dumps(self.marker_events.popleft()).encode()
    raise BlockingIOError()

  def radar(self, age_ms, *, valid=True, flipped=False, error=False):
    point = NS(trackId=56, radarSource='frontRadar', measured=True, dRel=20., yRel=0., vRel=0.)
    radar = NS(points=[point], radarTrackFlipped=flipped, errors=NS(to_dict=lambda: {'canError': error}))
    stamp = BASE_NS + age_ms * MS
    self.radar_events.append(NS(logMonoTime=stamp, which=lambda: 'liveTracks', valid=valid, liveTracks=radar))

  def marker(self, frame, age_ms):
    self.marker_events.append(dict(lens='wide', frame=frame, capture_ns=BASE_NS + age_ms * MS))

  def sample(self, age_ms):
    with patch.object(context.time, 'monotonic_ns', return_value=BASE_NS + age_ms * MS):
      return self.context.sample()

  def test_hud_cadence_keeps_causal_samples_between_packets(self):
    for age_ms in (5, 56):
      self.radar(age_ms)
    self.assertIsNone(self.sample(100))
    for age_ms in (107, 158):
      self.radar(age_ms)
    self.marker(1, 136)
    self.marker(2, 186)
    markers = self.sample(200)
    self.assertEqual([m['radar_ns'] for m in markers], [BASE_NS + 107 * MS, BASE_NS + 158 * MS])
    self.assertEqual([m['points'] for m in markers], [[[56, 20., 0., 0.]]] * 2)
    self.assertEqual(markers[0]['radar_epoch'], markers[1]['radar_epoch'])

  def test_reenabled_reader_ignores_radar_and_camera_from_before_activation(self):
    self.context.close()
    self.radar(70)
    self.marker(1, 80)
    self.context = self.open_context(100)
    self.marker(2, 110)
    self.assertIsNone(self.sample(120))
    self.radar(125)
    self.marker(3, 99)
    self.marker(4, 130)
    markers = self.sample(135)
    self.assertEqual([m['frame'] for m in markers], [4])
    self.assertEqual(markers[0]['radar_ns'], BASE_NS + 125 * MS)

  def test_radar_fault_discards_already_paired_markers_before_recovery(self):
    self.radar(45)
    self.marker(1, 55)
    initial = self.sample(70)
    self.assertEqual([m['frame'] for m in initial], [1])
    self.radar(95, valid=False, error=True)
    self.radar(145)
    self.marker(2, 105)
    self.marker(3, 155)
    recovered = self.sample(170)
    self.assertEqual([m['frame'] for m in recovered], [3])
    self.assertGreater(recovered[0]['radar_epoch'], initial[0]['radar_epoch'])

  def test_backlog_cannot_publish_before_pending_fault_is_read(self):
    self.radar(5)
    self.marker(1, 15)
    self.assertEqual([m['frame'] for m in self.sample(20)], [1])
    for age_ms in range(25, 25 + 10 * context.DRAIN_LIMIT, 10):
      self.radar(age_ms)
    self.radar(105, valid=False)
    self.marker(2, 75)
    before = self.radar_reads
    self.assertIsNone(self.sample(110))
    self.assertEqual(self.radar_reads - before, context.DRAIN_LIMIT + 1)
    self.radar(155)
    self.marker(3, 165)
    self.assertEqual([m['frame'] for m in self.sample(180)], [3])

  def test_stale_future_and_reversed_events_cannot_seed_history(self):
    self.radar(50)
    self.marker(1, 55)
    self.assertEqual([m['frame'] for m in self.sample(70)], [1])
    self.radar(400)
    self.radar(90)
    self.radar(89)
    self.marker(2, 92)
    self.assertIsNone(self.sample(100))
    self.radar(150, flipped=True)
    self.marker(3, 155)
    self.assertIsNone(self.sample(170))
    self.radar(200)
    self.marker(4, 205)
    self.assertIsNone(self.sample(500))


if __name__ == '__main__':
  unittest.main()
