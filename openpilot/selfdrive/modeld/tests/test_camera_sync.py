import os
import time
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.camera_sync import receive_camera_pair


class Camera:
  def __init__(self, frames):
    self.frames = iter(frames)
    self.frame_id = self.timestamp_sof = self.timestamp_eof = 0

  def recv(self):
    frame = next(self.frames, None)
    if frame is None:
      return None
    self.frame_id, self.timestamp_sof = frame
    self.timestamp_eof = self.timestamp_sof + 1_000_000
    return SimpleNamespace(frame_id=self.frame_id)


def test_alternating_exposure_intervals_do_not_drop_valid_pairs():
  # Ioniq 5 f79: short/long exposure spacing must still produce every pair.
  stamps = [1_000_000_000]
  for interval in [23_107_000, 77_653_000, 22_292_000, 75_255_000, 24_781_000, 75_103_000, 26_025_000]:
    stamps.append(stamps[-1] + interval)
  main = Camera(enumerate(stamps))
  extra = Camera((i, t + 100_000) for i, t in enumerate(stamps))
  for i in range(len(stamps)):
    buf, meta, wide, wide_meta = receive_camera_pair(main, extra)
    assert buf.frame_id == meta.frame_id == wide.frame_id == wide_meta.frame_id == i


def test_current_pair_resync_advances_only_older_camera():
  main = Camera([(10, 100_000_000), (11, 150_000_000)])
  extra = Camera([(20, 50_000_000), (21, 150_000_000)])
  _, meta, _, wide_meta = receive_camera_pair(main, extra)
  assert meta.frame_id == 11 and wide_meta.frame_id == 21


def test_timeout_never_reuses_a_stale_buffer():
  main = Camera([(0, 100_000_000)])
  extra = Camera([(0, 0)])
  assert receive_camera_pair(main, extra) is None
  assert receive_camera_pair(main, extra) is None


def test_single_camera_accepts_each_new_frame():
  main = Camera([(0, 100), (1, 200)])
  for i in range(2):
    buf, meta, extra, extra_meta = receive_camera_pair(main)
    assert buf is extra and meta is extra_meta and meta.frame_id == i


def test_resync_is_bounded_even_if_timestamps_never_match():
  main = Camera((i, i * 100_000_000) for i in range(100))
  extra = Camera((i, i * 100_000_000 + 50_000_000) for i in range(100))
  assert receive_camera_pair(main, extra) is None
  assert main.frame_id + extra.frame_id <= 10


@pytest.mark.parametrize('frame_id,main_sof,wide_sof', [
  (12212, 688_852_340_000, 688_865_358_000),
  (12232, 689_852_248_000, 689_864_863_000),
  (12266, 691_552_169_000, 691_565_355_000),
  (12793, 717_914_040_000, 717_927_115_000),
])
def test_ev9_complete_camera_streams_do_not_create_model_frame_gaps(frame_id, main_sof, wide_sof):
  # EV9 000002cc--03d0a44f7d--10: the old 10 ms limit skipped these
  # main frames, publishing invalid odometry despite complete camera streams.
  main = Camera([(frame_id - 1, main_sof - 50_000_000), (frame_id, main_sof),
                 (frame_id + 1, main_sof + 50_000_000)])
  wide = Camera([(frame_id, main_sof - 50_000_000), (frame_id + 1, wide_sof),
                 (frame_id + 2, main_sof + 50_000_000)])
  selected = [receive_camera_pair(main, wide)[1].frame_id for _ in range(3)]
  assert selected == [frame_id - 1, frame_id, frame_id + 1]


@pytest.mark.parametrize('skew', [-20_000_000, 20_000_000])
def test_small_skew_is_symmetric_and_bounded(skew):
  main = Camera([(1, 100_000_000)])
  wide = Camera([(1, 100_000_000 + skew)])
  assert receive_camera_pair(main, wide) is not None


@pytest.mark.parametrize('skew', [-20_000_001, 20_000_001])
def test_outside_skew_limit_requires_a_fresh_frame(skew):
  main = Camera([(1, 100_000_000)])
  wide = Camera([(1, 100_000_000 + skew)])
  assert receive_camera_pair(main, wide) is None


@pytest.mark.parametrize('missing', [1, 3])
def test_missing_camera_frames_still_create_a_real_gap(missing):
  # Ioniq f8c/f8d: phase slip / IFE recovery cannot be made valid by
  # accepting a camera pair separated by one or more complete periods.
  main = Camera((i, 1_000_000_000 + i * 50_000_000) for i in range(missing + 3))
  wide = Camera((i, 1_000_000_000 + i * 50_000_000) for i in [0, missing + 1, missing + 2])
  selected = [receive_camera_pair(main, wide)[1].frame_id for _ in range(3)]
  assert selected == [0, missing + 1, missing + 2]


class QueuedCamera(Camera):
  def __init__(self, frames):
    super().__init__(frames)
    self.waits = []

  def recv(self, timeout_ms=100):
    self.waits.append(timeout_ms)
    return super().recv()


@pytest.mark.parametrize('lag', [10, 11, 18, 28])
def test_pv5_recovers_first_aligned_pair_before_reading_next_road_frame(lag, monkeypatch):
  # Replay a wide queue with one packet per 50 ms exposure. The main
  # subscriber is conflated, so a fresh recv there loses the original road.
  monkeypatch.setattr('openpilot.selfdrive.modeld.camera_sync._boot_time_ns', lambda: lag * 50_000_000 + 30_000_000)

  def run(recover):
    road = QueuedCamera([(lag, lag * 50_000_000)])
    wide = QueuedCamera((i, i * 50_000_000) for i in range(lag + 1))
    pair = receive_camera_pair(road, wide, recover_backlog=recover)
    assert road.waits == [100]
    return pair, wide.waits

  assert run(False)[0] is None
  pair, waits = run(True)
  assert pair[1].frame_id == pair[3].frame_id == lag
  assert waits == [100] * 11 + [0] * (lag - 10)


@pytest.mark.parametrize('age', [-1, 250_000_000, 1_000_000_000])
def test_pv5_queue_recovery_rejects_future_or_expired_pair(age, monkeypatch):
  stamp = 10 * 50_000_000
  monkeypatch.setattr('openpilot.selfdrive.modeld.camera_sync._boot_time_ns', lambda: stamp + age)
  road = QueuedCamera([(10, stamp)])
  wide = QueuedCamera((i, i * 50_000_000) for i in range(11))
  assert receive_camera_pair(road, wide, recover_backlog=True) is None


@pytest.mark.parametrize(('lag', 'old_missed'), [(0, 0), (10, 1), (18, 1), (28, 2)])
def test_pv5_first_processed_exposure_at_20hz(lag, old_missed, monkeypatch):
  # The road camera delivers the next exposure only when the reader tries
  # again. Wide has lag older notifications waiting before the aligned one.
  for recovery, expected in ((False, old_missed), (True, 0)):
    road = QueuedCamera((i, i * 50_000_000) for i in range(lag, lag + 3))
    wide = QueuedCamera((i, i * 50_000_000) for i in range(lag + 3))
    monkeypatch.setattr('openpilot.selfdrive.modeld.camera_sync._boot_time_ns', lambda: road.timestamp_sof + 30_000_000)
    for _ in range(expected):
      assert receive_camera_pair(road, wide, recover_backlog=recovery) is None
    pair = receive_camera_pair(road, wide, recover_backlog=recovery)
    assert pair[1].frame_id == pair[3].frame_id == lag + expected


def test_pv5_queue_scan_has_a_bound_and_does_not_wait_when_queue_is_empty():
  road = QueuedCamera([(31, 31 * 50_000_000)])
  wide = QueuedCamera((i, i * 50_000_000) for i in range(32))
  assert receive_camera_pair(road, wide, recover_backlog=True) is None
  assert wide.waits == [100] * 11 + [0] * 18
  wide = QueuedCamera((i, i * 50_000_000) for i in range(11))
  road = QueuedCamera([(31, 31 * 50_000_000)])
  assert receive_camera_pair(road, wide, recover_backlog=True) is None
  assert wide.waits == [100] * 11 + [0]


@pytest.mark.parametrize('lag', [0, 12])
def test_pv5_rejects_overwritten_camera_ring_slot(lag):
  class Overwritten(QueuedCamera):
    def recv(self, timeout_ms=100):
      buf = super().recv(timeout_ms)
      if buf is not None and buf.frame_id == lag:
        buf.frame_id += 18
      return buf

  wide = Overwritten((i, i * 50_000_000) for i in range(lag + 1))
  road = QueuedCamera([(lag, lag * 50_000_000)])
  assert receive_camera_pair(road, wide, recover_backlog=True) is None


@pytest.mark.parametrize('road_id', [0, 18])
def test_pv5_uses_current_pixels_in_a_native_wide_backlog(road_id):
  vipc = pytest.importorskip('msgq.visionipc', exc_type=ImportError)
  road = vipc.VisionStreamType.VISION_STREAM_ROAD
  wide = vipc.VisionStreamType.VISION_STREAM_WIDE_ROAD
  name = f'pv5queue{os.getpid()}_{road_id}'
  server = vipc.VisionIpcServer(name)
  server.create_buffers(road, 2, 16, 16)
  server.create_buffers(wide, 18, 16, 16)
  server.start_listener()
  main = vipc.VisionIpcClient(name, road, True)
  extra = vipc.VisionIpcClient(name, wide, False)
  assert main.connect(True) and extra.connect(True)
  # Force reuse of the oldest wide slot before modeld consumes its packet.
  first = time.clock_gettime_ns(time.CLOCK_BOOTTIME) - 18 * 50_000_000
  for i in range(19):
    sof = first + i * 50_000_000
    pixels = bytes([i]) * (16 * 16 * 3 // 2)
    server.send(wide, pixels, i, sof, sof + 1)
    if i == road_id:
      server.send(road, pixels, i, sof, sof + 1)
  pair = receive_camera_pair(main, extra, recover_backlog=True)
  if road_id == 0:
    assert pair is None
  else:
    assert pair[1].frame_id == pair[3].frame_id == pair[0].data[0] == pair[2].data[0] == road_id
