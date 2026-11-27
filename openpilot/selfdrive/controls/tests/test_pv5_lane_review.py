from types import SimpleNamespace

import pytest

from openpilot.selfdrive.controls.lib.desire_lib.pv5_lane_review import Pv5LaneReview


class ReviewTimeline:
  def __init__(self, side):
    self.observer = Pv5LaneReview()
    self.side = side
    self.now = 10_000_000_000
    self.boot_offset = 1_000_000_000
    self.marking = SimpleNamespace(cameraMonoTime=0, leftCameraCode=0, rightCameraCode=0)
    self.carstate = SimpleNamespace(canValid=True, canTimeout=False, leftBlinker=False,
                                    rightBlinker=False, pv5LaneMarking=self.marking)

  def sample(self, code, *, signal=True, elapsed=50, oem_age=0,
             target_clear=True, valid=True, kind=None, confidence=0.95, sof=None, result_side=None):
    self.now += elapsed * 1_000_000
    boot_ns = self.now + self.boot_offset
    self.marking.cameraMonoTime = boot_ns - oem_age * 1_000_000
    field = "leftCameraCode" if self.side == 1 else "rightCameraCode"
    setattr(self.marking, field, code)
    direction = self.side if signal else 0
    self.carstate.leftBlinker = direction == 1
    self.carstate.rightBlinker = direction == 2
    visual = None if kind is None else (
      self.now - 10_000_000 if sof is None else sof,
      self.side if result_side is None else result_side, kind, confidence,
    )
    self.observer.update(self.carstate, now_ns=self.now, boot_ns=boot_ns, inputs_valid=valid,
                         driver_direction=direction, target_clear=target_clear, visual=visual)
    return self.observer.request_direction, self.observer.decision


@pytest.mark.parametrize("side", [1, 2])
@pytest.mark.parametrize("invisible", [0, 1])
def test_camera_loss_requests_only_the_driver_side_then_requires_two_distinct_visual_observations(side, invisible):
  timeline = ReviewTimeline(side)
  assert timeline.sample(8, signal=False) == (0, "idle")
  assert timeline.sample(8) == (0, "waitingForOemLoss")
  assert timeline.sample(invisible) == (side, "waitingForVisual")
  assert timeline.sample(invisible, kind=0) == (side, "waitingForVisual")
  first = timeline.now - 10_000_000
  assert timeline.sample(invisible, kind=0, sof=first) == (side, "waitingForVisual")
  assert timeline.sample(invisible, kind=0) == (side, "visualDashed")
  assert timeline.sample(8) == (0, "waitingForOemLoss")


@pytest.mark.parametrize("pre_edge", [-1, 0, 1, 2, 4, 5])
def test_only_original_dash_before_signal_can_arm_review(pre_edge):
  timeline = ReviewTimeline(1)
  assert timeline.sample(pre_edge, signal=False) == (0, "idle")
  assert timeline.sample(8) == (0, "idle")
  assert timeline.sample(0) == (0, "idle")


@pytest.mark.parametrize("code,kwargs", [
  (2, {}), (4, {}), (5, {}), (-1, {}), (0, {"oem_age": 251}),
  (0, {"oem_age": -1}), (0, {"oem_age": 50}), (0, {"target_clear": False}),
  (0, {"valid": False}), (0, {"elapsed": 251}),
])
def test_stale_missing_contradictory_or_ineligible_edge_retires_episode(code, kwargs):
  timeline = ReviewTimeline(1)
  timeline.sample(8, signal=False)
  assert timeline.sample(code, **kwargs) == (0, "idle")
  assert timeline.sample(0, kind=0) == (0, "idle")


@pytest.mark.parametrize("lane_type,display_code", [(1, -1), (4, -1), (-1, 11), (0, 20), (5, 35)])
def test_fresh_other_oem_evidence_blocks_loss_even_when_camera_packet_is_hidden(lane_type, display_code):
  timeline = ReviewTimeline(2)
  timeline.sample(8, signal=False)
  timeline.marking.rightType = lane_type
  timeline.marking.rightDisplayCode = display_code
  timeline.marking.monoTime = timeline.now + timeline.boot_offset
  timeline.marking.displayMonoTime = timeline.now
  assert timeline.sample(1) == (0, "idle")


def test_visual_unknown_age_wrong_side_and_low_confidence_cannot_complete_dash():
  timeline = ReviewTimeline(1)
  timeline.sample(8, signal=False)
  timeline.sample(1)
  assert timeline.sample(1, kind=0) == (1, "waitingForVisual")
  assert timeline.sample(1, kind=-1) == (1, "visualUnknown")
  assert timeline.sample(1, kind=0, result_side=2) == (1, "visualUnknown")
  assert timeline.sample(1, kind=0, confidence=0.84) == (1, "visualUnknown")
  assert timeline.sample(1, kind=0) == (1, "waitingForVisual")
  assert timeline.sample(1, kind=0, sof=timeline.now - 210_000_000) == (1, "waitingForVisual")
  assert timeline.sample(1, kind=0) == (1, "visualDashed")
  for _ in range(5):
    timeline.sample(1)
  assert timeline.sample(1) == (1, "waitingForVisual")


def test_visual_solid_retires_review_until_blinker_dark_and_a_new_oem_dash():
  timeline = ReviewTimeline(1)
  timeline.sample(8, signal=False)
  timeline.sample(0)
  assert timeline.sample(0, kind=1) == (0, "visualSolid")
  assert timeline.sample(0, kind=0) == (0, "visualSolid")
  assert timeline.sample(0, signal=False) == (0, "idle")
  assert timeline.sample(0) == (0, "idle")
  timeline.sample(8, signal=False)
  assert timeline.sample(0) == (1, "waitingForVisual")


def test_rapid_direction_change_and_input_invalidation_drop_the_pre_edge_snapshot():
  timeline = ReviewTimeline(1)
  timeline.sample(8, signal=False)
  timeline.sample(0)
  timeline.side = 2
  assert timeline.sample(0, kind=0) == (0, "idle")
  timeline.sample(8, signal=False)
  timeline.sample(8, signal=False, valid=False)
  assert timeline.sample(0) == (0, "idle")


def test_same_signal_episode_expires_even_with_current_valid_oem_absence():
  timeline = ReviewTimeline(1)
  timeline.sample(8, signal=False)
  timeline.sample(1)
  for _ in range(5):
    assert timeline.sample(1, elapsed=200) == (1, "waitingForVisual")
  assert timeline.sample(1) == (0, "idle")


@pytest.mark.parametrize("interval,expected", [(250, "visualDashed"), (350, "visualDashed"), (450, "waitingForVisual")])
def test_jetson_sampling_cadence_and_reply_latency_are_distinct_from_result_age(interval, expected):
  timeline = ReviewTimeline(1)
  timeline.sample(8, signal=False)
  timeline.sample(1)
  first_sof = timeline.now + 50_000_000
  assert timeline.sample(1, elapsed=150, kind=0, sof=first_sof) == (1, "waitingForVisual")
  assert timeline.sample(1, elapsed=200) == (1, "waitingForVisual")
  second_sof = first_sof + interval * 1_000_000
  assert timeline.sample(1, elapsed=interval - 200, kind=0, sof=second_sof) == (1, expected)
  timeline.sample(1, elapsed=100)
  assert timeline.sample(1, elapsed=51) == (1, "waitingForVisual")
