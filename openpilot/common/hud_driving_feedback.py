import math


LANE_CENTER_STATUS = {
  'waiting': ('중앙 보정 대기', 'Center · Ready'),
  'inactive': ('중앙 · 제어 꺼짐', 'Center · Inactive'),
  'stopped': ('중앙 · 정차', 'Center · Stopped'),
  'laneMode': ('중앙 · 레인모드', 'Center · Lane mode'),
  'blindspot': ('중앙 · BSD 대기', 'Center · BSD hold'),
  'inputs': ('중앙 · 입력 대기', 'Center · Inputs'),
  'driver': ('중앙 · 운전자', 'Center · Driver'),
  'maneuver': ('중앙 · 방향 전환', 'Center · Maneuver'),
  'lead': ('중앙 · 앞차 감지', 'Center · Lead'),
  'radar': ('중앙 · 레이더', 'Center · Radar'),
  'laneQuality': ('중앙 · 차선 신뢰도', 'Center · Lane quality'),
  'geometry': ('중앙 · 경로 조건', 'Center · Geometry'),
  'checking': ('중앙 · 조건 확인', 'Center · Checking'),
  'centered': ('중앙 · 보정 불필요', 'Center · Aligned'),
}


def model_confidence_score(model) -> float | None:
  """Display score before smoothing; it is not a calibrated probability."""
  try:
    predictions = model.meta.disengagePredictions
    score = 1.0
    for values in (predictions.brakeDisengageProbs, predictions.steerOverrideProbs):
      if values is None or len(values) != 5 or not all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0 for v in values):
        return None
      score *= 1.0 - max(values)
    return score
  except (AttributeError, TypeError):
    return None


def angle_steering_demand(speed: float, desired_curvature: float, roll: float, max_lateral_accel: float) -> float | None:
  if not all(map(math.isfinite, (speed, desired_curvature, roll, max_lateral_accel))) or max_lateral_accel <= 0:
    return None
  speed = max(0.0, speed)
  roll_weight = min(1.0, max(0.0, (speed - 5.0) / 10.0))
  demand = (desired_curvature * speed * speed - roll * 9.80665 * roll_weight) / max_lateral_accel
  return min(1.0, max(-1.0, demand))
