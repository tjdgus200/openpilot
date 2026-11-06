import math


EXTERNAL_NAVI_SOURCES = frozenset((
  "cam", "section", "bump", "police", "waze", "road", "atc", "atc2", "route",
))
VEHICLE_NAVI_SOURCES = frozenset(("hda", "hda_section", "hda_bump", "school"))

DECELERATION_SOURCE_LABELS = {
  "cam": "cam",
  "section": "section",
  "bump": "bump",
  "police": "police",
  "waze": "waze",
  "road": "road",
  "atc": "turn",
  "atc2": "turn",
  "route": "route",
  "hda": "cam",
  "hda_section": "section",
  "hda_bump": "bump",
  "school": "school",
  "gas": "gas",
  "vturn": "vturn",
  "model": "model",
  "turn": "turn",
}


def is_vehicle_navigation_source(source: str | None) -> bool:
  normalized = str(source or "").strip().lower()
  return normalized in VEHICLE_NAVI_SOURCES or normalized in ("cam:v", "bump:v", "school:v")


def deceleration_source_presentation(source: str | None) -> tuple[str, int]:
  """Return the actual deceleration reason and its source color mode.

  Color mode 2 is the normal deceleration orange, mode 3 is vehicle CAN
  navigation lavender, and mode 4 is external-navigation orange.
  """
  normalized = str(source or "").strip().lower()
  if not normalized:
    return "apply", 2
  if is_vehicle_navigation_source(normalized):
    return DECELERATION_SOURCE_LABELS.get(normalized, normalized[:8]), 3
  if normalized in EXTERNAL_NAVI_SOURCES or normalized.endswith(":n"):
    base = normalized.removesuffix(":n")
    return DECELERATION_SOURCE_LABELS.get(base, base[:8]), 4
  if normalized.endswith(":v"):
    base = normalized.removesuffix(":v")
    mode = 3 if base in ("cam", "section", "bump", "school") else 2
    return DECELERATION_SOURCE_LABELS.get(base, base[:8]), mode
  if normalized.endswith(":c"):
    base = normalized.removesuffix(":c")
    return DECELERATION_SOURCE_LABELS.get(base, base[:8]), 2
  return DECELERATION_SOURCE_LABELS.get(normalized, normalized[:8]), 2


DECELERATION_BANNER_LABELS = {
  "vturn": "커브 감속",
  "model": "모델 경로 감속",
  "turn": "회전 감속",
  "route": "경로 감속",
  "road": "제한속도 적용",
  "waze": "제한속도 적용",
  "cam": "단속 구간 감속",
  "section": "구간 단속 감속",
  "bump": "방지턱 감속",
  "school": "보호구역 감속",
}


def deceleration_banner_text(carrot, speed, set_speed, plan_target, acceleration):
  label, _ = deceleration_source_presentation(carrot.desiredSource)
  text = DECELERATION_BANNER_LABELS.get(label)
  desired_speed = carrot.desiredSpeed
  if text is None or not all(value is not None and math.isfinite(value)
                             for value in (speed, set_speed, desired_speed, plan_target, acceleration)):
    return None
  if not (0 < desired_speed < min(200, set_speed - 0.5) and abs(plan_target - desired_speed) <= 0.5
          and speed > plan_target + 0.5 and acceleration < -0.05):
    return None

  if label in ("cam", "waze"):
    target = getattr(carrot, "decelCameraTargetKph", 0)
    if 0 < target < 200:
      distance = getattr(carrot, "decelCameraDistanceM", 0)
      if distance > 0:
        return f"과속카메라 단속 {target:.0f} km/h 감속 중\n{distance:.0f} m 남음"
      return f"과속카메라 감속 중\n목표 {target:.0f} km/h"
  elif label in ("vturn", "route"):
    text = f"커브 감속 중\n목표 {desired_speed:.0f} km/h"
  return text


def navigation_status_presentation(vehicle_available: bool, external_active: bool) -> tuple[str, int] | None:
  """Return the navigation availability badge, independent of speed control."""
  if external_active:
    return "NAVI", 4
  if vehicle_available:
    return "vNAVI", 3
  return None


def external_navigation_connected(legacy_remote: str | None, carrot_navi_connected: bool) -> bool:
  """Return external-navigation connectivity without consulting speed-control state."""
  return bool(str(legacy_remote or "").strip()) or bool(carrot_navi_connected)
