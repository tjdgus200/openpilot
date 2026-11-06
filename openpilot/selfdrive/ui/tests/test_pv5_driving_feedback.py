from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pyray as rl

from openpilot.cereal import log
from openpilot.common.hud_driving_feedback import DRIVING_POPUP_ENTER_SECONDS, STEERING_WARNING_SERVICES, pv5_steering_warning
from openpilot.selfdrive.ui.onroad import pv5_driving_feedback as feedback
from openpilot.selfdrive.ui.onroad.pv5_driving_feedback import (
  Pv5DrivingFeedbackOverlay, sample_deceleration_popup, sample_driving_feedback,
)


class DrivingMessages:
  def __init__(self):
    services = ('modelV2', 'longitudinalPlan', 'carrotMan', *STEERING_WARNING_SERVICES)
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


def test_popup_slides_once_and_urgent_or_vehicle_alert_takes_priority(monkeypatch):
  state = SimpleNamespace(CP=SimpleNamespace(carFingerprint='KIA_PV5', notCar=False),
                          started=True, started_frame=10, sm=braking_messages())
  overlay = Pv5DrivingFeedbackOverlay()
  rect = rl.Rectangle(30, 30, 2100, 1020)
  now, warning = 100., 0
  frames = []
  monkeypatch.setattr(feedback.time, 'monotonic', lambda: now)
  monkeypatch.setattr(feedback, 'pv5_steering_warning', lambda *args, **kwargs: warning)
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded', lambda box, *_: frames.append((box.y, box.width, box.height)))
  monkeypatch.setattr(feedback.rl, 'draw_rectangle_rounded_lines_ex', lambda *_: None)
  with patch.object(feedback, 'draw_text_ui_style') as label:
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1] == (-48., 1080., 136.)
    assert label.call_args.args[0] == '방지턱 감속'
    now += DRIVING_POPUP_ENTER_SECONDS / 2
    warning = 1
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1][0] == pytest.approx(41.25)
    assert label.call_args.args[0] == '조향 요구량 높음'
    warning = 2
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1][0] == 54.
    assert label.call_args.args[0] == '전방 주시 · 조향 요구량 높음'
    overlay.warning = True
    overlay.draw_popup(rect, None, state, 80.)
    assert len(frames) == 3
    overlay.warning = False
    warning = 1
    overlay.draw_popup(rect, None, state, 80.)
    assert frames[-1][0] == -48.
