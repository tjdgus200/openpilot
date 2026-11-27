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

  def test_quiet_or_disabled_requests_release_gpu_and_can_rearm(self):
    closed = threading.Event()
    fake = mock.Mock()
    fake.close.side_effect = closed.set
    now = [1_000_000_000]
    w = LaneReview(lambda: fake, lambda: now[0])
    output = np.ones(536, np.float32)
    with mock.patch('pv5_lane_worker.IDLE_RELEASE_S', .05):
      try:
        w.set_side(0, 1)
        w.offer(1, now[0], None, SLICES, output, True)
        self.assertIsNone(w.thread)
        w.set_side(1, 2)
        w.offer(2, now[0], None, SLICES, output, True)
        self.assertTrue(w.ready.wait(2))
        first = w.thread
        w.set_side(0, 3)
        self.assertIsNone(w.latest())
        self.assertTrue(closed.wait(2))
        first.join(2)
        now[0] += SAMPLE_NS
        w.set_side(2, 4)
        w.offer(4, now[0], None, SLICES, output, True)
        self.assertTrue(w.ready.wait(2))
        self.assertIsNot(w.thread, first)
      finally:
        w.close()
        if w.thread:
          w.thread.join(2)


class InstallTest(unittest.TestCase):
  def test_matching_bench_plan_auto_enables_and_respects_host_override(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      (root / 'SOURCE_COMMIT').write_text('a' * 40)
      (root / runtime.PLAN).write_bytes(b'test plan')
      sha = hashlib.sha256(b'test plan').hexdigest()
      (root / 'validated.json').write_text(json.dumps({'release': 'a' * 40, 'bytes': 9, 'plan': sha, 'model': runtime.MODEL_SHA}))
      with (mock.patch.object(runtime, 'ROOT', root), mock.patch.object(runtime, 'PV5_PACKAGE', root / 'SOURCE_COMMIT'),
            mock.patch.dict(os.environ, {'PV5_LANE_REVIEW': '0'})):
        runtime.configure_installed(root)
        self.assertEqual(os.environ['PV5_LANE_REVIEW'], '0')
        del os.environ['PV5_LANE_REVIEW']
        runtime.configure_installed(root)
        self.assertEqual(os.environ.pop('PV5_LANE_REVIEW'), '1')
        self.assertEqual(runtime.installed_plan(root), (root / runtime.PLAN, sha))
        (root / 'SOURCE_COMMIT').write_text('b' * 40)
        self.assertIsNone(runtime.installed_plan(root))
        runtime.configure_installed(root)
        self.assertNotIn('PV5_LANE_REVIEW', os.environ)

  def test_release_revalidates_cached_plan_and_preserves_rollback(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      cache = root / 'cache'
      commit = root / 'SOURCE_COMMIT'
      model = root / 'lane.onnx'
      model.write_bytes(b'lane model fixture')
      sha = hashlib.sha256(model.read_bytes()).hexdigest()
      stopped = mock.Mock(stdout='LoadState=loaded\nActiveState=inactive')
      def build(onnx, path):
        self.assertEqual(onnx, model.read_bytes())
        path.write_bytes(b'synthetic plan')
      def smoke(path, digest):
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
      with (mock.patch.object(runtime, 'ROOT', root), mock.patch.object(runtime, 'MODEL', model),
            mock.patch.object(runtime, 'MODEL_SHA', sha), mock.patch.object(runtime.subprocess, 'run', return_value=stopped),
            mock.patch.object(runtime, '_build', side_effect=build) as builds, mock.patch.object(runtime, '_smoke', side_effect=smoke) as check):
        commit.write_text('a' * 40)
        runtime.prepare(cache)
        first = runtime.installed_plan(cache)
        self.assertIsNotNone(first)
        self.assertEqual(first[0].read_bytes(), b'synthetic plan')
        commit.write_text('b' * 40)
        self.assertIsNone(runtime.installed_plan(cache))
        runtime.prepare(cache)
        self.assertEqual(first, runtime.installed_plan(cache))
        builds.assert_called_once()
        self.assertEqual(check.call_count, 2)
        commit.write_text('c' * 40)
        check.side_effect = ValueError('synthetic incompatible plan')
        with self.assertRaises(ValueError):
          runtime.prepare(cache)
        self.assertIsNone(runtime.installed_plan(cache))
        for revision in ('a', 'b'):
          commit.write_text(revision * 40)
          self.assertEqual(first, runtime.installed_plan(cache))

  def test_no_build_in_live_services_or_outside_optional_time_budget(self):
    with tempfile.TemporaryDirectory() as tmp:
      cache = Path(tmp)
      active = mock.Mock(stdout='LoadState=loaded\nActiveState=active')
      stopped = mock.Mock(stdout='LoadState=loaded\nActiveState=inactive')
      for states in ((active,), (stopped, active)):
        with mock.patch.object(runtime.subprocess, 'run', side_effect=states), self.assertRaises(RuntimeError):
          runtime.prepare(cache)
      paired = cache / 'provenance.json'
      with mock.patch.object(runtime.subprocess, 'run') as proc, mock.patch.object(runtime, 'PV5_PACKAGE', paired):
        self.assertFalse(runtime.prepare_optional(cache, time.monotonic() + 5))
        proc.assert_not_called()
        paired.touch()
        self.assertFalse(runtime.prepare_optional(cache, time.monotonic() - 1))
        proc.assert_not_called()
        proc.side_effect = runtime.subprocess.TimeoutExpired('optional lane check', 1)
        self.assertFalse(runtime.prepare_optional(cache, time.monotonic() + 5))
        cmd = proc.call_args.args[0]
        self.assertEqual(cmd[-2:], ['prepare', str(cache / runtime.CACHE.name)])
        self.assertLessEqual(proc.call_args.kwargs['timeout'], 5)


if __name__ == '__main__':
  unittest.main()
