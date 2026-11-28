import os

import pytest

from openpilot.selfdrive.modeld import helpers
from openpilot.selfdrive.modeld.camera_sync import receive_camera_pair


@pytest.mark.parametrize('first', ['road', 'wide'])
def test_late_second_camera_required_on_pv5(monkeypatch, first):
  advertised = iter([set(), {first}, {first}, {first}, {'road', 'wide'}])
  identity_reads = []
  monkeypatch.setattr(helpers.time, 'sleep', lambda _: None)

  def required():
    identity_reads.append(True)
    return True

  assert helpers.wait_for_vision_streams(lambda: next(advertised), 'road', 'wide', True, required) == ('road', True)
  assert len(identity_reads) == 1
  assert list(advertised) == []


@pytest.mark.parametrize(('available', 'wide_enabled', 'expected'), [
  ({'road', 'wide'}, True, ('road', True)),
  ({'road', 'wide'}, False, ('road', False)),
  ({'road'}, False, ('road', False)),
])
def test_unambiguous_streams_do_not_wait_for_fingerprinting(available, wide_enabled, expected):
  def unexpected():
    pytest.fail('unnecessary CarParams wait')

  assert helpers.wait_for_vision_streams(lambda: available, 'road', 'wide', wide_enabled, unexpected) == expected


@pytest.mark.parametrize('single', ['road', 'wide'])
def test_existing_single_camera_fallback_on_other_cars(single):
  assert helpers.wait_for_vision_streams(lambda: {single}, 'road', 'wide', True, lambda: False) == (single, False)


def test_single_stream_advertisement_is_refreshed_after_waiting_for_identification():
  streams = iter([{'road'}, {'road', 'wide'}])
  assert helpers.wait_for_vision_streams(lambda: next(streams), 'road', 'wide', True, lambda: False) == ('road', True)


def test_lost_wide_during_dual_operation_recovers_without_substituting_road():
  class Camera:
    def __init__(self, stamps):
      self.stamps = iter(stamps)
      self.timestamp_sof = self.timestamp_eof = self.frame_id = 0

    def recv(self):
      stamp = next(self.stamps)
      if stamp is None:
        return None
      self.timestamp_sof, self.timestamp_eof = stamp, stamp + 1_000_000
      self.frame_id += 1
      return self

  road = Camera([100_000_000, 150_000_000, 200_000_000])
  wide = Camera([100_000_000, None, 150_000_000, 200_000_000])
  assert receive_camera_pair(road, wide)[2] is wide
  assert receive_camera_pair(road, wide) is None
  road_buf, road_meta, wide_buf, wide_meta = receive_camera_pair(road, wide)
  assert (road_buf, wide_buf) == (road, wide)
  assert road_meta.timestamp_sof == wide_meta.timestamp_sof == 200_000_000


@pytest.mark.parametrize(('required', 'expected_attempts'), [(True, 3), (None, 3), (False, 2)])
def test_empty_fd_handshake_can_be_retried_before_models_load(monkeypatch, required, expected_attempts):
  class Client:
    attempts = 0
    width = height = None

    def connect(self, block):
      assert not block
      self.attempts += 1
      if self.attempts == 3:
        self.width = self.height = 16
      return self.attempts > 1

  waits, scope_calls = [], []
  monkeypatch.setattr(helpers.time, 'sleep', waits.append)
  client = Client()
  helpers.connect_vision_buffers(client, lambda: scope_calls.append(True) or required)
  assert client.attempts == expected_attempts
  assert waits == [0.1] * (expected_attempts - 1)
  assert scope_calls == [True]


def test_native_camera_generation_change_and_replacement():
  vipc = pytest.importorskip('msgq.visionipc', exc_type=ImportError)
  road, wide, driver = (vipc.VisionStreamType.VISION_STREAM_ROAD, vipc.VisionStreamType.VISION_STREAM_WIDE_ROAD,
                        vipc.VisionStreamType.VISION_STREAM_DRIVER)
  name = f'pv5camtest{os.getpid()}'
  frame = bytes(16 * 16 * 3 // 2)

  def server():
    publisher = vipc.VisionIpcServer(name)
    for stream in (road, wide, driver):
      publisher.create_buffers(stream, 2, 16, 16)
    publisher.start_listener()
    return publisher

  def clients():
    streams = [vipc.VisionIpcClient(name, stream, stream != wide) for stream in (road, wide, driver)]
    for client in streams:
      helpers.connect_vision_buffers(client, lambda: True)
    return streams

  def send(publisher, seq):
    for stream in (road, wide, driver):
      publisher.send(stream, frame, seq, seq * 50_000_000, seq * 50_000_000 + 1)

  publisher = server()
  main, extra, dm = clients()
  send(publisher, 30)
  assert receive_camera_pair(main, extra, recover_backlog=True)[1].frame_id == 30
  assert dm.recv() is not None
  assert main.recv(timeout_ms=0) is None and dm.recv(timeout_ms=0) is None
  assert not helpers.lost_vision_buffers(main, extra, dm)

  del publisher
  publisher = server()
  send(publisher, 1)
  # msgq re-registers the old reader at the current end of the new queue.
  assert main.recv(timeout_ms=0) is None and dm.recv(timeout_ms=0) is None
  assert not helpers.lost_vision_buffers(main, extra, dm)
  send(publisher, 2)
  assert receive_camera_pair(main, extra, recover_backlog=True) is None and dm.recv() is None
  assert helpers.lost_vision_buffers(main, extra) and helpers.lost_vision_buffers(dm)
  # The supervised replacement owns entirely new clients and model caches.
  main, extra, dm = clients()
  send(publisher, 3)
  _, main_meta, _, extra_meta = receive_camera_pair(main, extra, recover_backlog=True)
  assert (main_meta.frame_id, extra_meta.frame_id) == (3, 3)
  assert dm.recv() is not None and dm.frame_id == 3
  assert not helpers.lost_vision_buffers(main, extra, dm)


def test_native_empty_handshake_while_vehicle_identity_is_pending():
  vipc = pytest.importorskip('msgq.visionipc', exc_type=ImportError)
  name = f'pv5camstart{os.getpid()}'
  road, wide = vipc.VisionStreamType.VISION_STREAM_ROAD, vipc.VisionStreamType.VISION_STREAM_WIDE_ROAD
  publisher = vipc.VisionIpcServer(name)
  publisher.create_buffers(road, 2, 16, 16)
  publisher.start_listener()
  pending = vipc.VisionIpcClient(name, wide, False)
  empty_handshakes = []

  def unknown_vehicle():
    # Reproduce camerad replacing the previously advertised stream set while
    # the model client is connecting, before live CarParams are available.
    nonlocal publisher
    empty_handshakes.append(pending.num_buffers)
    del publisher
    publisher = vipc.VisionIpcServer(name)
    publisher.create_buffers(road, 2, 16, 16)
    publisher.create_buffers(wide, 2, 16, 16)
    publisher.start_listener()
    return None

  helpers.connect_vision_buffers(pending, unknown_vehicle)
  assert empty_handshakes == [0]
  assert (pending.num_buffers, pending.width, pending.height) == (2, 16, 16)
