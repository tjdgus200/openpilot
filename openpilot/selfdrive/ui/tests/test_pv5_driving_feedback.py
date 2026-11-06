from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pyray as rl

from openpilot.cereal import log
from openpilot.common.hud_attention_icon import DriverAttentionIcon
from openpilot.common.hud_driving_feedback import (
  DRIVING_POPUP_ENTER_SECONDS, DRIVING_POPUP_MOTION_SECONDS, STEERING_WARNING_SERVICES,
  DrivingNoticeTracker, DrivingPopupSlide, driving_notice_events, driving_notice_text, driving_popup_colors, driving_popup_motion,
  pv5_steering_warning,
)
from openpilot.selfdrive.ui.onroad import pv5_driving_feedback as feedback
from openpilot.selfdrive.ui.onroad.pv5_driving_feedback import (
  Pv5DrivingFeedbackOverlay, sample_deceleration_popup, sample_driving_feedback,
)


class DrivingMessages:
  def __init__(self):
    services = ('modelV2', 'longitudinalPlan', 'carrotMan', 'radarState', *STEERING_WARNING_SERVICES)
    self.messages = {name: log.Event.new_message().init(name) for name in services}
    self.seen = dict.fromkeys(services, True)
    self.alive = self.seen.copy()
    self.valid = self.seen.copy()
    self.recv_frame = dict.fromkeys(services, 10)
    self.recv_time = dict.fromkeys(services, 100.0)
    self.logMonoTime = dict.fromkeys(services, 100_000_000_000)

  def __getitem__(self, service):
    return self.messages[service]


@pytest.mark.parametrize('direction', (-1, 1))
@pytest.mark.parametrize('demand,expected', ((0.79, 0), (0.8, 1), (0.89, 1), (0.9, 2), (1.0, 2)))
def test_steering_warning_thresholds(direction, demand, expected):
  sm = DrivingMessages()
  sm['carParams'].carFingerprint = 'KIA_PV5'
  sm['carParams'].maxLateralAccel = 4.0
  sm['carControl'].latActive = True
  sm['carState'].vEgo = 20.0
  dm = sm['driverMonitoringState']
  dm.activePolicy = 'vision'
  dm.visionPolicyState.faceDetected = True
  dm.visionPolicyState.isDistracted = True
  controls = sm['controlsState']
  controls.lateralControlState.init('angleState')
  controls.desiredCurvature = direction * demand / 100
  assert pv5_steering_warning(sm, 100.0) == expected

  controls.lateralControlState.init('torqueState')
  sm['carOutput'].actuatorsOutput.torque = direction * demand
  assert pv5_steering_warning(sm, 100.0) == expected


def test_steering_warning_requires_active_control_and_live_vision_for_escalation():
  sm = DrivingMessages()
  sm['carParams'].carFingerprint = 'KIA_PV5'
  sm['controlsState'].lateralControlState.init('torqueState')
  sm['carOutput'].actuatorsOutput.torque = 1.0
  sm['carControl'].latActive = True
  dm = sm['driverMonitoringState']
  dm.activePolicy = 'vision'
  dm.visionPolicyState.faceDetected = True
  assert pv5_steering_warning(sm, 100.0) == 1
  dm.visionPolicyState.isDistracted = True
  assert pv5_steering_warning(sm, 100.0) == 2
  sm.logMonoTime['driverMonitoringState'] -= 600_000_000
  assert pv5_steering_warning(sm, 100.0) == 1
  sm['carControl'].latActive = False
  assert pv5_steering_warning(sm, 100.0) == 0


def test_feedback_uses_live_model_steering_and_center_status():
  sm = DrivingMessages()
  model = sm['modelV2']
  model.confidence = 'red'  # The old disengagement grade must not affect the lane display.
  model.laneLineProbs = [0.1, 0.95, 0.82, 0.1]
  controls = sm['controlsState']
  controls.lateralControlState.init('angleState')
  controls.desiredCurvature = -0.005
  controls.pv5LaneCenterStatus = 'waiting'
  controls.pv5LaneCenterWaitReason = 'inputs'
  sm['carState'].vEgo = 10
  sm['carControl'].latActive = True

  data = sample_driving_feedback(sm, 10, 100.0, 3.0)
  assert data.left_lane_probability == pytest.approx(0.95)
  assert data.right_lane_probability == pytest.approx(0.82)
  assert data.lane_confidence == pytest.approx(0.82)
  assert data.steer == pytest.approx(-1 / 6)
  assert data.center_label == '중앙 · 입력 대기'
  assert data.center is None

  controls.pv5LaneCenterStatus = 'blindspot'
  assert sample_driving_feedback(sm, 10, 100.1, 3.0).center_label == '중앙 · BSD 대기'

  controls.pv5LaneCenterStatus = 'correcting'
  controls.pv5LaneCenterFraction = -0.75
  model.laneLineProbs = [1., 0.30, 0.92, 1.]
  data = sample_driving_feedback(sm, 10, 100.2, 3.0)
  assert data.lane_confidence == pytest.approx(0.30)
  assert data.center == -0.75


def test_stale_and_previous_drive_samples_are_not_displayed():
  sm = DrivingMessages()
  sm['controlsState'].pv5LaneCenterStatus = 'correcting'
  sm['controlsState'].pv5LaneCenterFraction = 0.75
  sm['modelV2'].laneLineProbs = [1., 0.95, 0.82, 1.]
  for data in (sample_driving_feedback(sm, 11, 100.0, 3.0), sample_driving_feedback(sm, 10, 100.6, 3.0)):
    assert data.left_lane_probability is None
    assert data.right_lane_probability is None
    assert data.lane_confidence is None
    assert data.steer is None
    assert data.center is None
    assert data.center_label == '중앙 보정 --'


def test_overlay_is_visible_only_onroad_on_a_real_pv5():
  cp = SimpleNamespace(carFingerprint='KIA_PV5', maxLateralAccel=3.0, notCar=False)
  ui_state = SimpleNamespace(started=False, started_frame=10, CP=cp, sm=DrivingMessages())
  overlay = Pv5DrivingFeedbackOverlay()
  with patch('openpilot.selfdrive.ui.onroad.pv5_driving_feedback._draw_feedback') as draw:
    overlay.draw(None, None, ui_state)
    ui_state.started = True
    cp.carFingerprint = 'OTHER'
    overlay.draw(None, None, ui_state)
    cp.carFingerprint = 'KIA_PV5'
    cp.notCar = True
    overlay.draw(None, None, ui_state)
    draw.assert_not_called()

    cp.notCar = False
    overlay.draw(None, None, ui_state)
    draw.assert_called_once()

    with patch('openpilot.selfdrive.ui.onroad.pv5_driving_feedback._draw_large_feedback') as large:
      overlay.large = True
      overlay.warning = True
      overlay.draw(None, None, ui_state)
      large.assert_not_called()
      overlay.warning = False
      overlay.draw(None, None, ui_state)
      large.assert_called_once()
      draw.assert_called_once()


def braking_messages():
  sm = DrivingMessages()
  sm['carControl'].longActive = True
  sm['carState'].vEgo = 20.
  sm['carrotMan'].desiredSource = 'hda_bump'
  sm['carrotMan'].desiredSpeed = 55.
  sm['longitudinalPlan'].cruiseTarget = 55.
  sm['longitudinalPlan'].aTarget = -.2
  return sm


def test_notices_use_fresh_confirmed_states_and_keep_detection_separate_from_braking():
  sm = braking_messages()
  sm['carState'].canValid = True
  sm['carState'].gearShifter = 'drive'
  radar = sm['radarState']
  radar.leadLeft.status = True
  radar.leadLeft.dRel = 10.
  radar.leadLeft.radar = True
  assert driving_notice_events(sm, 100., started_frame=10) == ()

  radar.leadCutInRisk.status = True
  radar.leadCutInRisk.radar = True
  radar.leadCutInRisk.dRel = 10.
  sm['carrotMan'].vTurnSpeed = -65
  assert driving_notice_events(sm, 100., started_frame=10) == ('cutin', 'curve')
  sm.logMonoTime['radarState'] += 200
  assert driving_notice_events(sm, 100.) == ('cutin', 'curve')
  sm.logMonoTime['radarState'] += 1_000_000
  assert driving_notice_events(sm, 100.) == ('curve',)
  sm.logMonoTime['radarState'] = 100_000_000_000

  sm['carControl'].latActive = True
  sm['modelV2'].meta.laneChangeDirection = 'left'
  sm['modelV2'].meta.laneChangeState = 'preLaneChange'
  assert driving_notice_events(sm, 100.) == ('cutin', 'curve')
  sm['modelV2'].meta.laneChangeState = 'laneChangeStarting'
  assert driving_notice_events(sm, 100.) == ('cutin', 'laneChange', 'curve')

  sm['longitudinalPlan'].longitudinalPlanSource = 'lead1'
  radar.leadTwo.status = True
  radar.leadTwo.dRel = 22.
  radar.leadTwo.aLeadK = -.6
  assert driving_notice_events(sm, 100.) == ('cutin', 'laneChange', 'leadBrake', 'curve')
  sm['carState'].gasPressed = True
  assert 'leadBrake' not in driving_notice_events(sm, 100.)
  sm['carState'].gasPressed = False
  sm['modelV2'].meta.laneChangeState = 'laneChangeFinishing'
  assert 'laneChange' in driving_notice_events(sm, 100.)
  sm['carControl'].latActive = False
  assert 'laneChange' not in driving_notice_events(sm, 100.)
  sm.logMonoTime['radarState'] -= 700_000_000
  assert driving_notice_events(sm, 100.) == ('curve',)
  assert driving_notice_events(sm, 100., started_frame=11) == ()
  sm['carState'].gearShifter = 'park'
  assert driving_notice_events(sm, 100.) == ()


def test_notice_onset_priority_expiry_and_no_replay_behind_alerts():
  tracker = DrivingNoticeTracker()
  assert tracker.update(('curve',), 100.) == 'curve'
  assert tracker.update(('cutin', 'laneChange', 'curve'), 100.1, decelerating=True) == 'cutin'
  assert tracker.update(('laneChange', 'curve'), 100.2) is None
  assert tracker.update(('laneChange', 'curve'), 100.3, blocked=True) is None
  assert tracker.update(('laneChange', 'curve'), 100.4) is None
  assert tracker.update((), 101.) is None
  assert tracker.update(('laneChange',), 101.2) == 'laneChange'
  for now in (101.6, 102., 102.4, 102.8, 103.1):
    assert tracker.update(('laneChange',), now) == 'laneChange'
  assert tracker.update(('laneChange',), 103.3) is None
  assert tracker.update(('laneChange',), 103.5) is None


@pytest.mark.parametrize('name', ('driverDistracted', 'driverUnresponsive'))
def test_driver_attention_icon_is_immediate_and_animates_once_per_stage(name):
  icon = DriverAttentionIcon()
  assert icon.scale('controlsMismatch/immediateDisable', 99.) == 0.
  assert icon.scale(f'{name}1/permanent', 100.) == pytest.approx(.82)
  assert .82 < icon.scale(f'{name}1/permanent', 100.16) < 1.
  assert icon.scale(f'{name}1/permanent', 100.6) == icon.scale(f'{name}1/permanent', 104.) == 1.
  assert icon.scale(f'{name}2/permanent', 104.1) == pytest.approx(.82)
  assert icon.scale(f'{name}3/permanent', 104.2) == pytest.approx(.82)
  assert icon.scale('', 104.4) == 0.
  assert icon.scale(f'{name}3/permanent', 105.) == pytest.approx(.82)


def test_popup_motion_settles_and_uses_actual_lane_change_direction():
  slide = DrivingPopupSlide()
  start = slide.motion_dots('camera', 100.)
  halfway = slide.motion_dots('camera', 100. + DRIVING_POPUP_MOTION_SECONDS / 2)
  settled = slide.motion_dots('camera', 101.)
  assert start != halfway != settled and len(settled) == 12
  assert slide.motion_dots('camera', 102.) is settled
  assert slide.motion_dots('cutin', 103.) == start
  assert slide.motion_dots(None, 103.1) == ()
  assert slide.motion_dots('cutin', 104.) == start
  assert driving_popup_motion(None, 'road', None) is None
  assert driving_popup_motion(None, 'bump', None) == 'bump'
  assert driving_popup_motion('laneChange', 'cam', None) is None
  left = driving_popup_motion('laneChange', 'cam', 'left')
  right = driving_popup_motion('laneChange', 'cam', 'right')
  slide.motion_dots(left, 105.)
  left_dots = slide.motion_dots(left, 106.)
  slide.motion_dots(right, 107.)
  assert slide.motion_dots(right, 108.) == tuple((-x, y) for x, y in left_dots)
  assert driving_popup_colors(0, left) == driving_popup_colors(0, right)
  assert len({driving_popup_colors(0, motion) for motion in ('camera', 'cutin', left)}) == 3
  assert driving_popup_colors(1, 'cutin') == ((77, 46, 9, 255), (255, 184, 66, 255))
  assert driving_popup_colors(2, left) == ((156, 18, 27, 255), (255, 78, 85, 255))


def test_comma_popup_draws_lane_change_only_after_start(monkeypatch):
  sm = braking_messages()
  state = SimpleNamespace(CP=SimpleNamespace(carFingerprint='KIA_PV5', notCar=False),
                          started=True, started_frame=10, sm=sm)
  sm['carState'].canValid = True
  sm['carState'].gearShifter = 'drive'
  sm['carControl'].latActive = True
  sm['modelV2'].meta.laneChangeDirection = 'left'
  sm['modelV2'].meta.laneChangeState = 'preLaneChange'
  overlay = Pv5DrivingFeedbackOverlay()
  now = 100.
  monkeypatch.setattr(feedback.time, 'monotonic', lambda: now)
  monkeypatch.setattr(feedback, 'pv5_steering_warning', lambda *args, **kwargs: 0)
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded', lambda *_: None)
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded_lines_ex', lambda *_: None)
  monkeypatch.setattr(feedback.rl, 'draw_circle_v', lambda *_: None)
  rect = rl.Rectangle(30, 30, 2100, 1020)
  with patch.object(feedback, 'draw_text_ui_style') as label:
    overlay.draw_popup(rect, None, state, None)
    label.assert_not_called()
    sm['modelV2'].meta.laneChangeState = 'laneChangeStarting'
    overlay.draw_popup(rect, None, state, None)
    assert label.call_args.args[0] == '자동 차선 변경 중'
    sm['modelV2'].meta.laneChangeState = 'off'
    now = 100.30
    overlay.draw_popup(rect, None, state, None)
    assert label.call_args.args[0] == '자동 차선 변경 중'
    assert label.call_args.args[2] == 122.
    now = 101.29
    overlay.draw_popup(rect, None, state, None)
    assert label.call_args.args[0] == '자동 차선 변경 중'
    now = 101.30
    label.reset_mock()
    overlay.draw_popup(rect, None, state, None)
    label.assert_not_called()


def test_comma_deceleration_uses_active_plan_and_fresh_inputs():
  sm = braking_messages()
  assert sample_deceleration_popup(sm, 10, 100., 80.) == '방지턱 감속'
  assert sample_deceleration_popup(sm, 10, 100., None) is None
  assert sample_deceleration_popup(sm, 11, 100., 80.) is None
  sm.recv_time['carrotMan'] = 98.
  assert sample_deceleration_popup(sm, 10, 100., 80.) is None
  sm.recv_time['carrotMan'] = 100.
  sm['longitudinalPlan'].cruiseTarget = 50.
  assert sample_deceleration_popup(sm, 10, 100., 80.) is None
  sm['longitudinalPlan'].cruiseTarget = 55.
  sm['carControl'].longActive = False
  assert sample_deceleration_popup(sm, 10, 100., 80.) is None


@pytest.mark.parametrize('source', ('cam', 'hda', 'waze'))
def test_camera_popup_uses_final_camera_target_and_live_distance(source, monkeypatch):
  sm = braking_messages()
  carrot = sm['carrotMan']
  carrot.desiredSource = source
  carrot.decelCameraTargetKph = 50
  carrot.decelCameraDistanceM = 250
  state = SimpleNamespace(CP=SimpleNamespace(carFingerprint='KIA_PV5', notCar=False),
                          started=True, started_frame=10, sm=sm, jetlink_badge=('jetSON', 'active'))
  monkeypatch.setattr(feedback.time, 'monotonic', lambda: 100.)
  right_edges = []
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded', lambda box, *_: right_edges.append(box.x + box.width))
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded_lines_ex', lambda *_: None)
  monkeypatch.setattr(feedback.rl, 'draw_circle_v', lambda *_: None)
  overlay = Pv5DrivingFeedbackOverlay()
  with patch.object(feedback, 'draw_text_ui_style') as draw:
    overlay.draw_popup(rl.Rectangle(30, 30, 2100, 1020), None, state, 80.)
    title, detail = (call.args for call in draw.call_args_list)
    assert title[0] == '과속카메라 단속 50 km/h 감속 중'
    assert detail[0] == '250 m 남음' and detail[2] > title[2]
    assert right_edges[0] < 1680
  carrot.decelCameraDistanceM = 120
  assert sample_deceleration_popup(sm, 10, 100., 80.).endswith('120 m 남음')
  carrot.decelCameraDistanceM = 0
  assert sample_deceleration_popup(sm, 10, 100., 80.) == '과속카메라 감속 중\n목표 50 km/h'
  carrot.decelCameraTargetKph = 0
  assert sample_deceleration_popup(sm, 10, 100., 80.) in ('단속 구간 감속', '제한속도 적용')


@pytest.mark.parametrize('source', ('vturn', 'route'))
def test_curve_popup_uses_applied_plan_target(source):
  sm = braking_messages()
  sm['carrotMan'].desiredSource = source
  sm['carrotMan'].vTurnSpeed = -45
  assert sample_deceleration_popup(sm, 10, 100., 80.) == '커브 감속 중\n목표 55 km/h'
  assert driving_notice_text('curve', -45) == '전방 커브 접근\n예상 45 km/h'


def test_popup_slides_once_and_urgent_or_vehicle_alert_takes_priority(monkeypatch):
  state = SimpleNamespace(CP=SimpleNamespace(carFingerprint='KIA_PV5', notCar=False),
                          started=True, started_frame=10, sm=braking_messages())
  overlay = Pv5DrivingFeedbackOverlay()
  rect = rl.Rectangle(30, 30, 2100, 1020)
  now, warning = 100., 0
  frames = []
  dots = []
  monkeypatch.setattr(feedback.time, 'monotonic', lambda: now)
  monkeypatch.setattr(feedback, 'pv5_steering_warning', lambda *args, **kwargs: warning)
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded', lambda box, *_: frames.append((box.y, box.width, box.height)))
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded_lines_ex', lambda *_: None)
  monkeypatch.setattr(feedback.rl, 'draw_circle_v', lambda *args: dots.append(args))
  with patch.object(feedback, 'draw_text_ui_style') as label:
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1] == (-48., 1164., 136.)
    assert len(dots) == 12
    assert label.call_args.args[0] == '방지턱 감속'
    now += DRIVING_POPUP_ENTER_SECONDS / 2
    warning = 1
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1][0] == pytest.approx(41.25)
    assert label.call_args.args[0] == '조향 요구량 높음'
    warning = 2
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1][0] == 54.
    assert label.call_args.args[0] == '전방 주시, 조향 요구량 높음'
    assert len(dots) == 12
    warning = 1
    overlay.draw_popup(rect, None, state, 80.)
    assert label.call_args.args[0] == '전방 주시, 조향 요구량 높음'
    overlay.warning = True
    overlay.draw_popup(rect, None, state, 80.)
    assert len(frames) == 4
    overlay.warning = False
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1][0] == -48.


def test_popup_hold_does_not_restart_for_updated_distance_or_queue_lower_priority():
  slide = DrivingPopupSlide()
  slide.update(100., deceleration='카메라 감속 중\n250 m 남음', decel_label='cam')
  assert slide.top(100., 24., 100., urgent=False) < 24.
  assert slide.update(100.1, deceleration='카메라 감속 중\n240 m 남음', decel_label='cam').text.endswith('240 m 남음')
  assert slide.update(100.12, notice='curve').motion == 'camera'
  assert slide.top(100.3, 24., 100., urgent=False) == 24.
  assert slide.update(101., deceleration='카메라 감속 중\n200 m 남음', decel_label='cam').text.endswith('200 m 남음')
  assert slide.top(101., 24., 100., urgent=False) == 24.
  assert slide.update(101.29).motion == 'camera'
  assert slide.update(101.30) is None
  assert slide.update(101.40) is None
