import math
from dataclasses import dataclass


STEERING_WARNING_SERVICES = ('carParams', 'carControl', 'controlsState', 'carState',
                             'liveParameters', 'carOutput', 'driverMonitoringState')
STEERING_WARNING_TEXT = {1: '조향 요구량 높음', 2: '전방 주시, 조향 요구량 높음',
                         3: '조향각 한계\n직접 조향하세요'}
DRIVING_POPUP_COLORS = {
  0: ((31, 70, 143, 255), (177, 220, 255, 255)),
  1: ((77, 46, 9, 255), (255, 184, 66, 255)),
  2: ((156, 18, 27, 255), (255, 78, 85, 255)),
  3: ((156, 18, 27, 255), (255, 78, 85, 255)),
}
_TRAFFIC_POPUP_COLORS = ((80, 46, 126, 255), (221, 190, 255, 255))
_LANE_POPUP_COLORS = ((7, 89, 86, 255), (115, 251, 224, 255))
DRIVING_POPUP_ENTER_SECONDS = 0.22
DRIVING_POPUP_HOLD_SECONDS = 1.0
DRIVING_POPUP_EXIT_SECONDS = 0.22
DRIVING_POPUP_MOTION_SECONDS = 0.48
DRIVING_NOTICE_TEXT = {
  'cutin': '옆차 끼어들기 경계 중',
  'cutinTrack': '끼어들기 차량\n선행차로 추적 중',
  'cutinBrake': '끼어들기 차량\n감속 반영 중',
  'laneChange': '자동 차선 변경 중',
  'leadBrake': '앞차 감속 반영',
  'curve': '전방 커브 접근',
}
DRIVING_NOTICE_OVER_DECEL = ('cutinBrake', 'cutinTrack', 'cutin', 'laneChange')
_POPUP_PRIORITY = {name: rank for rank, name in enumerate((*DRIVING_NOTICE_OVER_DECEL, 'deceleration', 'leadBrake', 'curve'))}

# Coordinates are normalized to the icon radius. Precompute the static shapes
# so the popup does no interpolation after the entrance settles.
_MOTION_RING = tuple((math.cos(i * math.tau / 12), math.sin(i * math.tau / 12)) for i in range(12))
_LANE_ARROW = ((-.9, -.6), (-.65, -.3), (-.4, 0), (-.65, .3), (-.9, .6),
               (0., -.8), (.35, -.5), (.65, -.25), (.95, 0.), (.65, .25), (.35, .5), (0., .8))
_MOTION_DOTS = {
  'camera': tuple((.88 * x, .88 * y) for x, y in _MOTION_RING[:8]) +
            ((-.6, -.10), (-.2, -.10), (.2, -.10), (.6, -.10)),
  'cutin': ((-.95, -.85), (-.7, -.55), (-.45, -.28), (-.23, 0.),
            (.95, -.85), (.7, -.55), (.45, -.28), (.23, 0.),
            (-.23, .34), (.23, .34), (-.23, .72), (.23, .72)),
  'laneRight': _LANE_ARROW,
  'laneLeft': tuple((-x, y) for x, y in _LANE_ARROW),
  'leadBrake': tuple((x, y) for y in (-.68, 0., .68) for x in (-.82, -.27, .27, .82)),
  'curve': tuple((.56 * math.sin(i * math.tau / 11), .95 - 1.9 * i / 11) for i in range(12)),
  'bump': tuple((-1. + 2. * i / 11, -.75 * math.sin(i * math.pi / 11)) for i in range(12)),
  'section': tuple((x, y) for x in (-.67, .67) for y in (-.85, -.51, -.17, .17, .51, .85)),
}
_DECELERATION_MOTION = {
  'cam': 'camera', 'waze': 'camera', 'vturn': 'curve', 'route': 'curve',
  'turn': 'curve', 'model': 'curve', 'bump': 'bump', 'section': 'section', 'school': 'section',
}
_MOTION_DOTS['cutinTrack'] = _MOTION_DOTS['cutin']
_MOTION_DOTS['cutinBrake'] = _MOTION_DOTS['leadBrake']


def driving_popup_colors(warning: int, motion: str | None):
  if warning in STEERING_WARNING_TEXT:
    return DRIVING_POPUP_COLORS[warning]
  if motion in ('cutin', 'cutinTrack', 'cutinBrake', 'leadBrake'):
    return _TRAFFIC_POPUP_COLORS
  if motion in ('laneLeft', 'laneRight'):
    return _LANE_POPUP_COLORS
  return DRIVING_POPUP_COLORS[0]


def driving_popup_motion(notice: str | None, deceleration_label: str | None, lane_direction: str | None) -> str | None:
  if notice == 'laneChange':
    return {'left': 'laneLeft', 'right': 'laneRight'}.get(lane_direction)
  if notice is not None:
    return notice if notice in _MOTION_DOTS else None
  return _DECELERATION_MOTION.get(deceleration_label)


def driving_notice_text(notice: str | None, curve_speed: float | None) -> str | None:
  text = DRIVING_NOTICE_TEXT.get(notice)
  if notice == 'curve' and curve_speed is not None and 0 < abs(curve_speed) < 250:
    return f'{text}\n예상 {abs(curve_speed):.0f} km/h'
  return text


@dataclass(frozen=True)
class DrivingPopup:
  text: str
  warning: int
  motion: str | None
  priority: int


class DrivingPopupSlide:
  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self._popup = None
    self._hold_until = None
    self._entered = None
    self._exit_started = None
    self._motion_key = None
    self._motion_started = 0.

  def update(self, now: float, *, warning: int = 0, notice: str | None = None, curve_speed: float | None = None,
             deceleration: str | None = None, decel_label: str | None = None,
             lane_direction: str | None = None) -> DrivingPopup | None:
    warning_text = STEERING_WARNING_TEXT.get(warning)
    text = warning_text or driving_notice_text(notice, curve_speed) or deceleration
    if self._exit_started is not None and now >= self._exit_started + DRIVING_POPUP_EXIT_SECONDS:
      self.reset()
    current = self._popup
    if text:
      priority = -warning if warning_text else _POPUP_PRIORITY[notice or 'deceleration']
      motion = None if warning_text else driving_popup_motion(notice, decel_label, lane_direction)
      # Distance and target updates refresh the current popup without restarting its hold.
      if current is not None and self._exit_started is None and (priority, motion) == (current.priority, current.motion):
        if text != current.text:
          self._popup = DrivingPopup(text, warning, motion, priority)
        return self._popup
    if (current is not None and (not text or priority >= current.priority)
        and (self._hold_until is None or now < self._hold_until)):
      return current
    if not text:
      if current is not None and self._exit_started is None:
        self._exit_started = now
      return current
    self._popup = DrivingPopup(text, warning, motion, priority)
    self._hold_until = None
    self._exit_started = None
    return self._popup

  def motion_dots(self, key: str | None, now: float) -> tuple[tuple[float, float], ...]:
    if key != self._motion_key:
      self._motion_key = key
      self._motion_started = now
    if key is None:
      return ()
    target = _MOTION_DOTS[key]
    elapsed = now - self._motion_started
    if elapsed >= DRIVING_POPUP_MOTION_SECONDS:
      return target
    progress = 1. - (1. - max(0., elapsed) / DRIVING_POPUP_MOTION_SECONDS) ** 3
    return tuple((x + (end_x - x) * progress, y + (end_y - y) * progress)
                 for (x, y), (end_x, end_y) in zip(_MOTION_RING, target, strict=True))

  def top(self, now: float, top: float, height: float, *, urgent: bool, clip_top: float | None = None) -> float:
    if self._exit_started is not None:
      progress = min(1., max(0., (now - self._exit_started) / DRIVING_POPUP_EXIT_SECONDS))
      distance = height if clip_top is None else height + top - clip_top
      return top - distance * progress ** 3
    if urgent:
      self._entered = now - DRIVING_POPUP_ENTER_SECONDS
    if self._entered is None:
      self._entered = now
    if urgent or now >= self._entered + DRIVING_POPUP_ENTER_SECONDS:
      if self._hold_until is None:
        self._hold_until = now + DRIVING_POPUP_HOLD_SECONDS
      return top
    remaining = max(0., 1. - (now - self._entered) / DRIVING_POPUP_ENTER_SECONDS)
    return top - height * .75 * remaining ** 3


class DrivingNoticeTracker:
  """Select the highest-priority notice while its fresh source remains active."""
  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self.active: str | None = None
    self._stage = None
    self._stage_since = 0.

  def update(self, events: tuple[str, ...], now: float, *, blocked=False, decelerating=False) -> str | None:
    stage = next((e for e in events if e in ('cutinBrake', 'cutinTrack')), None)
    if blocked or stage != self._stage or now < self._stage_since:
      self._stage, self._stage_since = stage, now
    # Confirm stage changes, not the initial risk warning. Never retain a stage
    # after its fresh predicate disappears or a vehicle alert preempts it.
    if stage is not None and (blocked or now - self._stage_since < .15):
      events = tuple('cutin' if e == stage else e for e in events)
    visible = events
    if blocked:
      visible = ()
    elif decelerating:
      visible = tuple(event for event in events if event in DRIVING_NOTICE_OVER_DECEL)
    self.active = min(visible, key=_POPUP_PRIORITY.__getitem__, default=None)
    return self.active


def driving_notice_events(sm, now: float, *, started_frame: int | None = None) -> tuple[str, ...]:
  """Read existing perception and planner states for display only."""
  def current(service, limit):
    # Jetlink translates a floating-point receive timestamp and an integer
    # event timestamp into the same monotonic clock. Allow rounding at zero age.
    if (sm.valid.get(service, False) and sm.alive.get(service, False)
        and (started_frame is None or sm.recv_frame.get(service, -1) >= started_frame)
        and -1e-6 <= now - sm.recv_time.get(service, 0.) <= limit
        and -1e-6 <= now - sm.logMonoTime.get(service, 0) * 1e-9 <= limit):
      return sm[service]
    return None

  car = current('carState', .3)
  if car is None or not car.canValid or car.canTimeout or str(car.gearShifter) != 'drive':
    return ()
  radar = current('radarState', .5)
  control = current('carControl', .3)
  model = current('modelV2', .5)
  events = []
  if radar is not None:
    def in_front(lead):
      return lead.status and math.isfinite(lead.dRel) and lead.dRel > 0

    risk = radar.leadCutInRisk
    risks = tuple(lead for lead in (risk, *radar.leadsCutIn) if lead.radar and in_front(lead))
    if risks:
      stage = 'cutin'
      plan = current('longitudinalPlan', .5)
      if control is not None and control.longActive and not car.gasPressed and plan is not None:
        source = str(plan.longitudinalPlanSource)
        lead = radar.leadOne if source == 'lead0' else radar.leadTwo if source == 'lead1' else None
        if (lead is not None and lead.radar and in_front(lead) and lead.radarTrackId >= 0
            and any(lead.radarTrackId == other.radarTrackId for other in risks)):
          stage = 'cutinTrack'
          if math.isfinite(plan.aTarget) and plan.aTarget < -.1 and math.isfinite(car.aEgo) and car.aEgo < -.1:
            stage = 'cutinBrake'
      events.append(stage)
  if control is not None and control.latActive and model is not None:
    if (str(model.meta.laneChangeState) in ('laneChangeStarting', 'laneChangeFinishing')
        and str(model.meta.laneChangeDirection) in ('left', 'right')):
      events.append('laneChange')
  if radar is not None and control is not None and control.longActive and not car.gasPressed:
    plan = current('longitudinalPlan', .5)
    if plan is not None and math.isfinite(plan.aTarget) and plan.aTarget < -.1:
      source = str(plan.longitudinalPlanSource)
      lead = radar.leadOne if source == 'lead0' else radar.leadTwo if source == 'lead1' else None
      if lead is not None and in_front(lead) and math.isfinite(lead.aLeadK) and lead.aLeadK < -.2:
        events.append('leadBrake')
  carrot = current('carrotMan', 1.)
  if model is not None and carrot is not None and math.isfinite(car.vEgo):
    ratio = car.vCluRatio if .5 < car.vCluRatio <= 1.2 else 1.
    curve_kph = abs(carrot.vTurnSpeed)
    if 0 < curve_kph < 250 and car.vEgo * 3.6 / ratio >= curve_kph + 5.:
      events.append('curve')
  return tuple(events)


LANE_CENTER_STATUS = {
  'waiting': ('차선 보정 대기', 'Edge · Ready'),
  'inactive': ('차선 · 제어 꺼짐', 'Edge · Inactive'),
  'stopped': ('차선 · 저속/정차', 'Edge · Low speed'),
  'laneMode': ('차선 · 레인모드', 'Edge · Lane mode'),
  'blindspot': ('차선 · BSD 대기', 'Edge · BSD hold'),
  'inputs': ('차선 · 입력 대기', 'Edge · Inputs'),
  'driver': ('차선 · 운전자', 'Edge · Driver'),
  'maneuver': ('차선 · 방향 전환', 'Edge · Maneuver'),
  'lead': ('차선 · 앞차 감지', 'Edge · Lead'),
  'radar': ('차선 · 레이더', 'Edge · Radar'),
  'laneQuality': ('차선 · 인식 대기', 'Edge · Lane quality'),
  'geometry': ('차선 · 경로 조건', 'Edge · Geometry'),
  'checking': ('차선 · 조건 확인', 'Edge · Checking'),
  'centered': ('차선 · 보정 불필요', 'Edge · Clear'),
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
  if (kind == 'angleState' and cp.steerControlType == 'angle'
      and fresh('carState', 0.3) and fresh('carOutput', 0.3)):
    cs = sm['carState']
    if (cs.canValid and not cs.steeringPressed and not cs.steerFaultTemporary and not cs.steerFaultPermanent
        and pv5_angle_limit_exceeded(sm['carControl'].actuators.steeringAngleDeg,
                                    sm['carOutput'].actuatorsOutput.steeringAngleDeg, cs.steeringAngleDeg)):
      return 3
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


def pv5_angle_limit_exceeded(requested: float, applied: float, measured: float) -> bool:
  """Read-only check of a planned angle beyond the configured Hyundai command cap.

  The request already comes from the lateral controller's model-based target.
  Do not equate the lateral-acceleration gauge or a rate-limited command with
  a steering-angle stop. Require both the applied and measured angles near
  the cap, plus a larger same-direction request; ignore manual override above.
  """
  if not all(math.isfinite(v) for v in (requested, applied, measured)):
    return False
  # This runs on comma. Jetson receives only the classified integer, not DM
  # data or a second controller. Missing vehicle code must fail closed.
  try:
    from opendbc.car.hyundai.values import CarControllerParams
  except ImportError:
    return False
  limit = CarControllerParams.ANGLE_LIMITS.STEER_ANGLE_MAX
  return (math.isfinite(limit) and limit > 3.0
          and abs(requested) > max(limit, abs(measured)) + 3.0
          and abs(abs(applied) - limit) <= 0.5 and abs(measured) >= limit - 3.0
          and requested * applied > 0 and requested * measured > 0)
