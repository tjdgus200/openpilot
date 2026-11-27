"""Focused PC geometry, scheduling and install-admission tests; no vehicle inputs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from pv5_lane_review import UNKNOWN, classify, road_luma, select_marking, target_crop
import pv5_lane_runtime as runtime
from pv5_lane_worker import LaneReview, RESULT_TTL_NS, SAMPLE_NS

LEFT = np.tile([-1.8, 1.4], (33, 1))
RIGHT = np.tile([1.8, 1.4], (33, 1))
SLICES = {'lane_lines': slice(0, 528), 'lane_lines_prob': slice(528, 536)}


def tensors():
  return {'output0': np.zeros((1, 42, 3549), np.float32),
          'output1': np.full((1, 32, 104, 104), -1., np.float32)}


def paint(data, anchor, cls, xs, ys, broad=False, offset=0):
  data['output0'][0, :4, anchor] = [208, 208, 416, 416]
  data['output0'][0, 4 + cls, anchor] = .95
  data['output0'][0, 10 + anchor, anchor] = 1.
  for x, y in zip(xs + offset, ys):
    radius = 20 if broad else 1
    data['output1'][0, anchor, y, max(0, x - radius):min(104, x + radius + 1)] = 1.


class GeometryTest(unittest.TestCase):
  def test_exact_plane_parities(self):
    img = np.arange(512 * 256, dtype=np.uint8).reshape(256, 512) + np.arange(256, dtype=np.uint8)[:, None]
    planes = np.stack([img[i % 2::2, i // 2::2] for i in range(4)])
    np.testing.assert_array_equal(road_luma(planes.tobytes()), img)

  def test_projection_and_supported_types(self):
    for side, coords, cls, expected in ((1, LEFT, 1, 0), (2, RIGHT, 5, 1)):
      crop, x, y, near = target_crop(side, coords, .9)
      self.assertEqual(crop, 0 if side == 1 else 256)
      self.assertGreater(np.ptp(y[near]), 5)
      outputs = tensors()
      paint(outputs, 0, cls, x, y)
      def forward(blob):
        self.assertEqual(blob.shape, (1, 3, 416, 416))
        np.testing.assert_equal(blob[0, 0], blob[0, 2])
        return outputs
      self.assertEqual(classify(np.zeros((256, 512), np.uint8), side, coords, .9, forward)[0], expected)

  def test_fail_unknown_for_missing_mixed_broad_weak_or_far(self):
    _, x, y, near = target_crop(1, LEFT, .9)
    for case in ('empty', 'far', 'mixed', 'broad', 'weak', 'wrongline', 'ignored', 'same_anchor', 'nonfinite'):
      outputs = tensors()
      if case == 'far':
        paint(outputs, 0, 1, x[~near], y[~near])
      elif case in ('mixed', 'broad', 'weak', 'wrongline', 'ignored', 'same_anchor'):
        paint(outputs, 0, 3 if case == 'ignored' else 1, x, y, broad=case == 'broad', offset=15 if case == 'wrongline' else 0)
        if case == 'mixed':
          paint(outputs, 1, 0, x[:2], y[:2])
        if case == 'same_anchor':
          outputs['output0'][0, 4, 0] = .9
        if case == 'weak':
          outputs['output0'][0, 5, 0] = .64
      elif case == 'nonfinite':
        outputs['output1'][0, 0, 1, 1] = np.nan
      self.assertEqual(select_marking(outputs, x, y, near), UNKNOWN, case)

  def test_bad_or_occluded_target_skips_onnx(self):
    for side, coords, prob in [(1, RIGHT, .9), (2, RIGHT, np.nan), (2, RIGHT, .49), (1, LEFT * 100, .99)]:
      with self.subTest(side=side, probability=prob):
        self.assertIsNone(target_crop(side, coords, prob))
        self.assertEqual(classify(np.zeros((256, 512), np.uint8), side, coords, prob, self.fail), UNKNOWN)


class WorkerTest(unittest.TestCase):
  def test_single_slot_expiry_cancellation_and_no_byte_alias(self):
    now = [1_000_000_000]
    entered, release = threading.Event(), threading.Event()
    received = []
    output = np.zeros(536, np.float32)
    output[:264].reshape(4, 33, 2)[1] = LEFT
    output[531] = 3.
    warped = np.zeros((2, 6, 128, 256), np.uint8)
    warped[1] = 253

    class Fake:
      def close(self): pass
    def detect(y, side, yz, p, forward):
      entered.set()
      release.wait(4.)
      received.append((y.max(), side, tuple(yz[0])))
      return 0, .95

    w = LaneReview(Fake, lambda: now[0])
    with mock.patch('pv5_lane_worker.classify', detect):
      try:
        def submit(frame):
          w.set_side(1, frame)
          w.offer(frame, now[0], warped, SLICES, output, True)
        submit(1)
        self.assertTrue(w.ready.wait(2.))
        now[0] += SAMPLE_NS
        submit(2)
        self.assertTrue(entered.wait(2.))
        warped[0] = 190
        output[:264] = 0.
        now[0] += SAMPLE_NS
        submit(3)
        self.assertEqual(w.mail.qsize(), 0)
        w.set_side(2, 4)
        release.set()
        deadline = time.monotonic() + 2.
        while not received and time.monotonic() < deadline:
          time.sleep(.001)
        self.assertEqual(received, [(0, 1, tuple(LEFT.astype(np.float32)[0]))])
        self.assertIsNone(w.latest())
        w.result = w.generation, now[0], (5, 2, 0, .9)
        self.assertIsNotNone(w.latest())
        now[0] += RESULT_TTL_NS
        self.assertIsNone(w.latest())
        w.set_side(1, 6)
        w.result = w.generation, now[0], (6, 1, 1, .9)
        output[531] = -1
        submit(7)
        self.assertIsNone(w.latest())
      finally:
        release.set()
        w.close()
        if w.thread:
          w.thread.join(2.)
          self.assertFalse(w.thread.is_alive())

  def test_late_or_failed_response_cannot_start_worker(self):
    w = LaneReview(self.fail, lambda: 200_000_000)
    w.set_side(1, 3)
    w.offer(3, 100_000_000, None, SLICES, np.ones(536), True)
    self.assertIsNone(w.thread)
    w.offer(3, 200_000_000, None, {}, None, False)
    self.assertEqual(w.side, 0)
    self.assertIsNone(w.latest())


class InstallTest(unittest.TestCase):
  def test_advertisement_requires_explicit_lab_setting_and_matching_validated_commit(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      (root / 'SOURCE_COMMIT').write_text('a' * 40)
      (root / runtime.PLAN).write_bytes(b'test plan')
      sha = hashlib.sha256(b'test plan').hexdigest()
      (root / 'validated.json').write_text(json.dumps({'release': 'a' * 40, 'bytes': 9, 'plan': sha, 'model': runtime.MODEL_SHA}))
      with mock.patch.object(runtime, 'ROOT', root), mock.patch.dict(os.environ, {'PV5_LANE_REVIEW': '0'}):
        self.assertIsNone(runtime.installed_plan(root))
        os.environ['PV5_LANE_REVIEW'] = '1'
        self.assertEqual(runtime.installed_plan(root), (root / runtime.PLAN, sha))
        (root / 'SOURCE_COMMIT').write_text('b' * 40)
        self.assertIsNone(runtime.installed_plan(root))


if __name__ == '__main__':
  unittest.main()
