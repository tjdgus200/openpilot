"""Xiaoge vision payload validation and car-state integration helpers."""

from dataclasses import dataclass
import json
from typing import Protocol


XIAOGE_LANE_TIMEOUT_NS = 4_000_000_000
XIAOGE_BLINDSPOT_TIMEOUT_NS = 1_500_000_000
XIAOGE_LANE_TYPES = (-1, 0, 1)


@dataclass(frozen=True)
class XiaogeVisionResult:
  left_lane: int
  right_lane: int
  lane_valid: bool
  lane_received_nanos: int
  left_blindspot: bool
  right_blindspot: bool
  blindspot_valid: bool
  blindspot_received_nanos: int
  left_blindspot_received_nanos: int = 0
  right_blindspot_received_nanos: int = 0
  remote_cpu: bool = False
  left_evaluated: bool = False
  right_evaluated: bool = False


class VisionState(Protocol):
  leftLaneLine: int
  rightLaneLine: int
  leftBlindspot: bool
  rightBlindspot: bool


def _lane_type(value: object, field: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value not in XIAOGE_LANE_TYPES:
    raise ValueError(f"{field} must be -1, 0, or 1")
  return value


def _bool(value: object, field: str) -> bool:
  if not isinstance(value, bool):
    raise ValueError(f"{field} must be a boolean")
  return value


def _received_nanos(value: object, field: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise ValueError(f"{field} must be a non-negative integer")
  return value


def parse_xiaoge_vision_payload(payload: bytes) -> XiaogeVisionResult:
  data = json.loads(payload)
  if not isinstance(data, dict) or data.get("type") != "xiaogeVision" or data.get("version") != 1:
    raise ValueError("expected a version 1 xiaogeVision object")
  lane = data.get("lane")
  blindspot = data.get("blindspot")
  if not isinstance(lane, dict) or not isinstance(blindspot, dict):
    raise ValueError("lane and blindspot must be objects")
  blindspot_received_nanos = _received_nanos(blindspot.get("receivedMonoTimeNanos"), "blindspot.receivedMonoTimeNanos")
  by_side = blindspot.get("receivedMonoTimeNanosBySide", {})
  if not isinstance(by_side, dict):
    raise ValueError("blindspot.receivedMonoTimeNanosBySide must be an object")
  valid_sides = blindspot.get('validSides', [])
  if not isinstance(valid_sides, list) or len(valid_sides) > 2 or any(side not in ('left', 'right') for side in valid_sides):
    raise ValueError('blindspot.validSides must list evaluated sides')
  return XiaogeVisionResult(
    _lane_type(lane.get("leftLine"), "lane.leftLine"),
    _lane_type(lane.get("rightLine"), "lane.rightLine"),
    _bool(lane.get("valid"), "lane.valid"),
    _received_nanos(lane.get("receivedMonoTimeNanos"), "lane.receivedMonoTimeNanos"),
    _bool(blindspot.get("left"), "blindspot.left"),
    _bool(blindspot.get("right"), "blindspot.right"),
    _bool(blindspot.get("valid"), "blindspot.valid"),
    blindspot_received_nanos,
    _received_nanos(by_side.get("left", blindspot_received_nanos), "blindspot.receivedMonoTimeNanosBySide.left"),
    _received_nanos(by_side.get("right", blindspot_received_nanos), "blindspot.receivedMonoTimeNanosBySide.right"),
    lane.get('processor') == 'jetson_cpu_wifi',
    'left' in valid_sides,
    'right' in valid_sides,
  )


def merge_xiaoge_lane_type(current: int, detected: int) -> int:
  """Replace only the lane-marking type while preserving a vehicle-provided color code."""
  if detected < 0:
    return current
  color = (current // 10) * 10 if current >= 10 else 0
  return color + detected


def _is_fresh(received_nanos: int, now_nanos: int, timeout_nanos: int) -> bool:
  age_nanos = now_nanos - received_nanos
  return received_nanos != 0 and 0 <= age_nanos <= timeout_nanos


def xiaoge_blindspot_vision(result: XiaogeVisionResult | None, now_nanos: int) -> tuple[bool, bool]:
  if result is None or not result.blindspot_valid or \
     not _is_fresh(result.blindspot_received_nanos, now_nanos, XIAOGE_BLINDSPOT_TIMEOUT_NS):
    return False, False
  left_received = result.left_blindspot_received_nanos or result.blindspot_received_nanos
  right_received = result.right_blindspot_received_nanos or result.blindspot_received_nanos
  return (
    result.left_blindspot and _is_fresh(left_received, now_nanos, XIAOGE_BLINDSPOT_TIMEOUT_NS),
    result.right_blindspot and _is_fresh(right_received, now_nanos, XIAOGE_BLINDSPOT_TIMEOUT_NS),
  )


def apply_xiaoge_vision_result(CS: VisionState, result: XiaogeVisionResult | None, now_nanos: int) -> bool:
  sources = getattr(CS, 'laneTypeSources', None)
  required = sources is not None and sources.visionRequired
  if required:
    # Camera age includes capture, preparation, WLAN and CPU time. A remote
    # completion must never renew the age of an old image.
    fresh = bool(result is not None and result.remote_cpu and result.lane_valid and
                 _is_fresh(result.lane_received_nanos, now_nanos, 1_000_000_000))
    sources.visionFresh = fresh
    for side in ('left', 'right'):
      current = int(getattr(CS, side + 'LaneLine'))
      if sources.oemTracked:
        oem_valid = bool(getattr(sources, side + 'OemValid'))
        current = int(getattr(sources, side + 'Oem')) if oem_valid else -1
      else:
        # Legacy producers have no validity bit; zero is ambiguous. Explicit
        # nonzero recognized markings are preserved conservatively.
        oem_valid = current >= 0 and current % 10 in (0, 1, 3, 4, 5, 7, 8) and current != 0
      detected = getattr(result, side + '_lane') if fresh else -1
      setattr(sources, side + 'Vision', detected)
      if oem_valid:
        effective = current
        if current % 10 in (0, 5) and detected == 1:
          effective = merge_xiaoge_lane_type(current, 1)
      else:
        effective = merge_xiaoge_lane_type(max(-1, current), detected) if detected >= 0 else -1
      setattr(CS, side + 'LaneLine', effective)
      side_stamp = getattr(result, side + '_blindspot_received_nanos') if result else 0
      checked = bool(result and getattr(result, side + '_evaluated') and result.blindspot_valid and
                     _is_fresh(side_stamp, now_nanos, XIAOGE_BLINDSPOT_TIMEOUT_NS))
      clear = bool(checked and not getattr(result, side + '_blindspot'))
      permitted_line = detected == 0 and (not oem_valid or current % 10 in (0, 5))
      if sources.oemTracked and (not sources.oemDiscoveryComplete or (sources.oemAvailable and not oem_valid)):
        permitted_line = False  # A capable vehicle's undecided/stale lane is not a dashed permission.
      if not sources.oemTracked and current >= 10 and not oem_valid:
        permitted_line = False  # Legacy colored-but-undecided native markings are also not permission.
      setattr(sources, side + 'AutoHold', not (fresh and permitted_line and clear))
    # Keep the ordinary OR-only BSD merge below, never clear OEM detection.
    if result is None:
      return True
  if result is None:
    return False

  applied = False
  if not required and result.lane_valid and _is_fresh(result.lane_received_nanos, now_nanos, XIAOGE_LANE_TIMEOUT_NS):
    if result.left_lane >= 0:
      CS.leftLaneLine = merge_xiaoge_lane_type(CS.leftLaneLine, result.left_lane)
      applied = True
    if result.right_lane >= 0:
      CS.rightLaneLine = merge_xiaoge_lane_type(CS.rightLaneLine, result.right_lane)
      applied = True

  left_vision, right_vision = xiaoge_blindspot_vision(result, now_nanos)
  if left_vision or right_vision:
    CS.leftBlindspot = bool(CS.leftBlindspot) or left_vision
    CS.rightBlindspot = bool(CS.rightBlindspot) or right_vision
    applied = True
  return applied
