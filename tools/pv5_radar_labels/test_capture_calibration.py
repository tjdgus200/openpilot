"""Capture metadata uses a causal timestamp for an unchanged projection."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from openpilot.common.pv5_radar_label_context import CaptureMarker, LENS_BYTES

MS = 1_000_000
T = 10_000 * MS


class CaptureCalibrationTest(unittest.TestCase):
  def setUp(self):
    with patch('socket.socket'), patch('socket.AF_UNIX', 1, create=True):
      self.marker = CaptureMarker()
    self.calibration = SimpleNamespace(height=[1.2], wideFromDeviceEuler=[0.] * 3, rpyCalib=[0.] * 3)
    self.warp = np.eye(3)
    self.intrinsics = np.array([[455., 0., 256.], [0., 455., 151.8], [0., 0., 1.]])
    self.images = bytes(2 * LENS_BYTES)

  def calibrate(self, ms, valid=True):
    self.marker.calibrate(self.calibration, T + ms * MS, self.intrinsics, self.warp, valid)

  def publish(self, ms):
    self.marker.sock.sendto.reset_mock()
    self.marker.identify(1, self.images)
    self.marker.publish(1, T + ms * MS)
    if self.marker.sock.sendto.called:
      return json.loads(self.marker.sock.sendto.call_args.args[0])
    return None

  def test_repeated_identical_projection_uses_previous_causal_stamp_only(self):
    self.calibrate(0)
    previously_applied = self.publish(10)['projection']
    self.calibrate(250)
    self.calibrate(250)  # Same calibration sampled again after settings poll.
    early = self.publish(235)
    self.assertEqual((early['calibration_ns'], early['projection']), (T, previously_applied))
    self.assertEqual(self.publish(-10)['calibration_ns'], T + 250 * MS)
    self.assertEqual(self.publish(250)['calibration_ns'], T + 250 * MS)
    self.calibrate(500)
    self.assertEqual(self.publish(490)['calibration_ns'], T + 250 * MS)

  def test_changed_warp_or_calibration_cannot_borrow_old_timestamp(self):
    self.calibrate(0)
    self.warp[0, 2] = 3.
    self.calibrate(250)
    self.assertEqual(self.publish(235)['calibration_ns'], T + 250 * MS)
    self.calibration.wideFromDeviceEuler = [0., .01, 0.]
    self.calibrate(500)
    self.assertEqual(self.publish(485)['calibration_ns'], T + 500 * MS)

  def test_invalid_rollback_and_stale_inputs_clear_fallback(self):
    self.calibrate(0)
    self.calibrate(250)
    self.calibrate(200)
    self.assertEqual(self.publish(185)['calibration_ns'], T + 200 * MS)
    self.calibrate(450)
    self.assertEqual(self.publish(435)['calibration_ns'], T + 200 * MS)
    self.calibrate(500, valid=False)
    self.assertIsNone(self.publish(505))
    self.calibrate(750)
    self.assertEqual(self.publish(735)['calibration_ns'], T + 750 * MS)
    self.calibrate(1850)
    self.assertEqual(self.publish(1800)['calibration_ns'], T + 1850 * MS)


if __name__ == '__main__':
  unittest.main()
