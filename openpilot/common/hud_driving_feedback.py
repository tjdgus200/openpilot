import math


STEERING_WARNING_SERVICES = ('carParams', 'carControl', 'controlsState', 'carState',
                             'liveParameters', 'carOutput', 'driverMonitoringState')
STEERING_WARNING_TEXT = {1: '조향 요구량 높음', 2: '전방 주시, 조향 요구량 높음'}
DRIVING_POPUP_COLORS = {
  0: ((16, 25, 35, 250), (84, 103, 122, 255)),
  1: ((77, 46, 9, 255), (255, 184, 66, 255)),
  2: ((156, 18, 27, 255), (255, 78, 85, 255)),
}
DRIVING_POPUP_ENTER_SECONDS = 0.22


class DrivingPopupSlide:
  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self._entered = None

  def top(self, now: float, top: float, height: float, *, urgent: bool) -> float:
    if urgent:
      self._entered = now - DRIVING_POPUP_ENTER_SECONDS
      return top
    if self._entered is None:
      self._entered = now
    remaining = max(0., 1. - (now - self._entered) / DRIVING_POPUP_ENTER_SECONDS)
    return top - height * .75 * remaining ** 3


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


def _valid_lane_probability(value) -> bool:
  return type(value) in (int, float) and 0.0 <= value <= 1.0


def lane_line_probabilities(model) -> tuple[float | None, float | None]:
  """Read the current lane's two model probabilities; never substitute a grade."""
  try:
    values = model.laneLineProbs
    if len(values) != 4:
      return None, None
    return tuple(v if _valid_lane_probability(v) else None for v in (values[1], values[2]))
  except (AttributeError, TypeError):
    return None, None


def lane_confidence_score(left: float | None, right: float | None) -> float | None:
  return min(left, right) if _valid_lane_probability(left) and _valid_lane_probability(right) else None


def lane_confidence_grade(score: float | None) -> str:
  # Display thresholds only; these do not change lateral admission.
  return '' if not _valid_lane_probability(score) else 'green' if score >= 0.85 else 'yellow' if score >= 0.5 else 'red'


def lane_probability_text(probability: float | None) -> str:
  return f'{probability:.0%}' if _valid_lane_probability(probability) else '--'


def angle_steering_demand(speed: float, desired_curvature: float, roll: float, max_lateral_accel: float) -> float | None:
  if not all(map(math.isfinite, (speed, desired_curvature, roll, max_lateral_accel))) or max_lateral_accel <= 0:
    return None
  speed = max(0.0, speed)
  roll_weight = min(1.0, max(0.0, (speed - 5.0) / 10.0))
  demand = (desired_curvature * speed * speed - roll * 9.80665 * roll_weight) / max_lateral_accel
  return min(1.0, max(-1.0, demand))


def pv5_steering_warning(sm, now: float, *, cp=None, started_frame: int | None = None) -> int:
  """Classify the display/audio advisory using live, active steering demand."""
  def fresh(name, max_age):
    return (sm.valid.get(name, False) and sm.alive.get(name, False)
            and (started_frame is None or sm.recv_frame[name] >= started_frame)
            and 0 <= now - sm.recv_time[name] <= max_age
            and 0 <= now - sm.logMonoTime[name] * 1e-9 <= max_age)

  if cp is None:
    if not sm.valid.get('carParams', False):
      return 0
    cp = sm['carParams']
  if cp.notCar or cp.carFingerprint != 'KIA_PV5':
    return 0
  if not (fresh('carControl', 0.3) and sm['carControl'].latActive and fresh('controlsState', 0.3)):
    return 0

  controls = sm['controlsState']
  kind = controls.lateralControlState.which()
  if kind == 'angleState' and fresh('carState', 0.3) and fresh('liveParameters', 0.5):
    demand = angle_steering_demand(sm['carState'].vEgo, controls.desiredCurvature or controls.curvature,
                                  sm['liveParameters'].roll, cp.maxLateralAccel)
  elif kind in ('torqueState', 'pidState') and fresh('carOutput', 0.3):
    demand = sm['carOutput'].actuatorsOutput.torque
  else:
    return 0
  if demand is None or not math.isfinite(demand):
    return 0
  # The source torque and curvature are encoded as Float32.
  magnitude = abs(demand) + 1e-7
  if magnitude < 0.8:
    return 0

  if magnitude >= 0.9 and fresh('driverMonitoringState', 0.5):
    dm = sm['driverMonitoringState']
    vision = dm.visionPolicyState
    if dm.activePolicy == 'vision' and not dm.cameraUnavailable and not dm.dm2Disabled and vision.faceDetected and vision.isDistracted:
      return 2
  return 1
