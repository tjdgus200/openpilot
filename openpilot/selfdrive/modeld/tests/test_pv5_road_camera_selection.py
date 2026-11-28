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
