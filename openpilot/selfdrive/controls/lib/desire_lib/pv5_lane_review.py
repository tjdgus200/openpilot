"""Shadow review of a fresh OEM marking loss after a driver signal."""

from opendbc.car.hyundai.lane_line_time import PV5_CLUSTER_DASHED, PV5_LANE_MARKING_MAX_AGE_NS


# The original camera still has to send a validated 0x161 packet. These are
# the DBC's GRAY and HIDDEN display states, either of which lacks a visible line.
_OEM_INVISIBLE = (0, 1)
_OEM_REVIEW_CODES = (*_OEM_INVISIBLE, PV5_CLUSTER_DASHED)
# Only review early loss; later changes may concern an entirely different boundary.
_REVIEW_WINDOW_NS = 1_000_000_000
_VISUAL_MAX_AGE_NS = 250_000_000
_VISUAL_CONTINUITY_NS = 400_000_000
_DASH_CONFIDENCE_MIN = 0.85


def _fresh(stamp, now_ns, max_age_ns):
  return stamp > 0 and 0 <= now_ns - stamp <= max_age_ns


def _contradicts_dashed(marking, direction, now_ns, boot_ns):
  if direction == 1:
    lane_type = getattr(marking, "leftType", -1)
    display_code = getattr(marking, "leftDisplayCode", -1)
  else:
    lane_type = getattr(marking, "rightType", -1)
    display_code = getattr(marking, "rightDisplayCode", -1)
  return (
    (_fresh(getattr(marking, "monoTime", 0), boot_ns, PV5_LANE_MARKING_MAX_AGE_NS)
     and lane_type not in (-1, 0, 5)) or
    (_fresh(getattr(marking, "displayMonoTime", 0), now_ns, PV5_LANE_MARKING_MAX_AGE_NS)
     and display_code not in (-1, 0, 10, 15))
  )


class Pv5LaneReview:
  def __init__(self):
    self.reset()

  def reset(self):
    self._last_update = 0
    self._clear()

  def _clear(self):
    self._before_signal = (0, False, False)
    self._signal_direction = 0
    self._signal_since = 0
    self._camera_stamp = 0
    self._review_since = 0
    self._visual_stamp = 0
    self._visual_dash_count = 0
    self.request_direction = 0
    self.decision = "idle"

  @property
  def signal_direction(self):
    return self._signal_direction

  def update(self, carstate, *, now_ns, boot_ns, inputs_valid, driver_direction, target_clear, visual=None):
    if (not inputs_valid or not carstate.canValid or carstate.canTimeout
        or (self._last_update and not 0 < now_ns - self._last_update <= PV5_LANE_MARKING_MAX_AGE_NS)):
      self.reset()
      return
    self._last_update = now_ns
    raw_direction = int(carstate.leftBlinker) + 2 * int(carstate.rightBlinker)
    marking = getattr(carstate, "pv5LaneMarking", None)
    stamp = getattr(marking, "cameraMonoTime", 0)
    if not _fresh(stamp, boot_ns, PV5_LANE_MARKING_MAX_AGE_NS):
      self._clear()
      return
    left = marking.leftCameraCode
    right = marking.rightCameraCode

    if not raw_direction:
      self._clear()
      self._before_signal = (stamp,
                             left == PV5_CLUSTER_DASHED and not _contradicts_dashed(marking, 1, now_ns, boot_ns),
                             right == PV5_CLUSTER_DASHED and not _contradicts_dashed(marking, 2, now_ns, boot_ns))
      return

    if raw_direction not in (1, 2) or raw_direction != driver_direction or not target_clear:
      self._clear()
      return

    if not self._signal_direction:
      before_stamp = self._before_signal[0]
      had_dashed = self._before_signal[raw_direction]
      self._before_signal = (0, False, False)
      if not had_dashed or not _fresh(before_stamp, boot_ns, PV5_LANE_MARKING_MAX_AGE_NS):
        return
      self._signal_direction = raw_direction
      self._signal_since = now_ns
      self._camera_stamp = before_stamp

    code = left if raw_direction == 1 else right
    if (self._signal_direction != raw_direction or now_ns - self._signal_since > _REVIEW_WINDOW_NS
        or stamp < self._camera_stamp or _contradicts_dashed(marking, raw_direction, now_ns, boot_ns)
        or code not in _OEM_REVIEW_CODES
        or (code in _OEM_INVISIBLE and not self._review_since and stamp == self._camera_stamp)):
      self._clear()
      return
    self._camera_stamp = stamp
    if self.decision == "visualSolid":
      return

    if code == PV5_CLUSTER_DASHED:
      self._review_since = self._visual_stamp = self._visual_dash_count = 0
      self.request_direction = 0
      self.decision = "waitingForOemLoss"
      return

    if not self._review_since:
      self._review_since = now_ns
    self.request_direction = raw_direction
    if not _fresh(self._visual_stamp, now_ns, _VISUAL_MAX_AGE_NS):
      self.decision = "waitingForVisual"
    if visual is None:
      return
    sof, side, kind, confidence = visual
    if (side != raw_direction or not _fresh(sof, now_ns, _VISUAL_MAX_AGE_NS)
        or sof < self._review_since or sof <= self._visual_stamp
        or kind not in (-1, 0, 1) or not 0 <= confidence <= 1):
      return
    continuous = sof - self._visual_stamp <= _VISUAL_CONTINUITY_NS
    self._visual_stamp = sof
    if kind == 1:
      # One fresh contrary visual observation retires the episode until signal-off.
      self.request_direction = 0
      self._visual_dash_count = 0
      self.decision = "visualSolid"
    elif kind == 0 and confidence >= _DASH_CONFIDENCE_MIN:
      self._visual_dash_count = min(2, self._visual_dash_count + 1) if continuous else 1
      self.decision = "visualDashed" if self._visual_dash_count == 2 else "waitingForVisual"
    else:
      self._visual_dash_count = 0
      self.decision = "visualUnknown"
