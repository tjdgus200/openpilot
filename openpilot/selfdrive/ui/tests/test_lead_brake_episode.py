import math

import pytest

from openpilot.common.hud_driving_feedback import (
  DrivingNoticeTracker, LeadBrakeEpisode, LeadBrakeSample, driving_notice_events, lead_brake_sample,
)
from openpilot.selfdrive.ui.tests.test_pv5_driving_feedback import DrivingMessages


def sample(track=7, plan=-1.2, lead=-1.3):
  return LeadBrakeSample(track, plan, lead)


def run(episode, start, end, value, allowed=True):
  return [episode.update(value, i / 10, allowed=allowed) for i in range(start, end)]


@pytest.mark.parametrize('plan,lead', [(-.2, -.6), (-.99, -2), (-2, -.99), (math.nan, -2), (-2, math.inf)])
def test_small_or_invalid_deceleration_never_displays(plan, lead):
  assert not any(run(LeadBrakeEpisode(), 0, 100, sample(plan=plan, lead=lead)))


def test_both_thresholds_need_confirmation_and_display_only_once():
  e = LeadBrakeEpisode()
  result = run(e, 0, 100, sample(plan=-1, lead=-1))
  assert result[:3] == [False] * 3
  assert result[3]
  assert sum(v and not result[i-1] for i, v in enumerate(result) if i) == 1
  assert not any(result[18:])


@pytest.mark.parametrize('interruption', ['weak', 'missing', 'blocked', 'target', 'gap'])
def test_interruption_does_not_replay_shown_episode(interruption):
  e = LeadBrakeEpisode()
  assert run(e, 0, 5, sample())[-1]
  if interruption == 'weak':
    e.update(sample(plan=-.5), .5, allowed=True)
  elif interruption == 'missing':
    e.update(None, .5, allowed=True)
  elif interruption == 'blocked':
    e.update(sample(), .5, allowed=False)
  elif interruption == 'target':
    e.update(sample(track=8), .5, allowed=True)
  else:
    e.update(sample(), 1.1, allowed=True)
  assert not any(run(e, 12, 50, sample()))


def test_new_target_can_display_but_returning_target_remains_latched():
  e = LeadBrakeEpisode()
  assert any(run(e, 0, 10, sample(7)))
  assert any(run(e, 10, 20, sample(8)))
  assert not any(run(e, 20, 40, sample(7)))


def test_continuous_observed_recovery_rearms_same_target():
  e = LeadBrakeEpisode()
  assert any(run(e, 0, 20, sample()))
  assert not any(run(e, 20, 42, sample(plan=-.3, lead=-.3)))
  assert any(run(e, 42, 50, sample()))


@pytest.mark.parametrize('recovery', [sample(plan=-.31, lead=0), sample(plan=0, lead=-.31), None])
def test_partial_or_unknown_recovery_never_rearms(recovery):
  e = LeadBrakeEpisode()
  assert any(run(e, 0, 20, sample()))
  run(e, 20, 60, recovery)
  assert not any(run(e, 60, 80, sample()))


def test_recovery_must_be_continuous():
  e = LeadBrakeEpisode()
  run(e, 0, 20, sample())
  run(e, 20, 35, sample(plan=0, lead=0))
  e.update(None, 3.5, allowed=True)
  run(e, 36, 51, sample(plan=0, lead=0))
  assert not any(run(e, 51, 70, sample()))


def test_preemption_before_display_does_not_consume_episode():
  e = LeadBrakeEpisode()
  assert not any(run(e, 0, 10, sample(), allowed=False))
  assert e.update(sample(), 1., allowed=True)


def test_priority_and_blocking_leave_urgent_notices_immediate():
  t = DrivingNoticeTracker()
  for i in range(10):
    assert t.update(('cutin', 'leadBrake'), i/10, lead_brake=sample()) == 'cutin'
  assert t.update(('leadBrake',), 1., lead_brake=sample()) == 'leadBrake'
  assert t.update(('leadBrake',), 1.1, lead_brake=sample(), blocked=True) is None
  assert t.update(('leadBrake',), 1.2, lead_brake=sample()) is None
  assert t.update(('cutin',), 1.3, decelerating=True) == 'cutin'


def test_reset_rearms_but_missing_unknown_identity_does_not():
  e = LeadBrakeEpisode()
  assert any(run(e, 0, 10, sample(None)))
  run(e, 10, 20, None)
  assert not any(run(e, 20, 30, sample(None)))
  e.reset()
  assert any(run(e, 30, 40, sample(None)))


def messages():
  sm = DrivingMessages()
  sm['carState'].canValid = True
  sm['carState'].gearShifter = 'drive'
  sm['carControl'].longActive = True
  sm['longitudinalPlan'].longitudinalPlanSource = 'lead1'
  sm['longitudinalPlan'].aTarget = -1.2
  lead = sm['radarState'].leadTwo
  lead.status, lead.radar = True, True
  lead.dRel, lead.aLeadK, lead.radarTrackId = 20., -1.3, 27
  return sm


def test_selected_lead_identity_and_mild_recovery_samples():
  sm = messages()
  assert lead_brake_sample(sm, 100.).track_id == 27
  assert 'leadBrake' in driving_notice_events(sm, 100.)
  sm['longitudinalPlan'].aTarget = -.2
  assert lead_brake_sample(sm, 100.) is not None
  assert 'leadBrake' not in driving_notice_events(sm, 100.)
  sm['radarState'].leadTwo.radar = False
  assert lead_brake_sample(sm, 100.).track_id is None


@pytest.mark.parametrize('service', ['carState', 'carControl', 'radarState', 'longitudinalPlan'])
@pytest.mark.parametrize('condition', ['invalid', 'dead', 'old_receive', 'old_source', 'old_trip'])
def test_stale_samples_cannot_trigger_or_rearm(service, condition):
  sm = messages()
  if condition == 'invalid': sm.valid[service] = False
  elif condition == 'dead': sm.alive[service] = False
  elif condition == 'old_receive': sm.recv_time[service] = 98.
  elif condition == 'old_source': sm.logMonoTime[service] = 98_000_000_000
  else: sm.recv_frame[service] = 9
  assert lead_brake_sample(sm, 100., started_frame=10) is None


@pytest.mark.parametrize('field,value', [('gasPressed', True), ('canValid', False), ('canTimeout', True), ('gearShifter', 'park')])
def test_driver_override_and_invalid_car_state_suppress_notice(field, value):
  sm = messages()
  setattr(sm['carState'], field, value)
  assert lead_brake_sample(sm, 100.) is None


def test_cluster_popup_uses_episode_and_retains_warning_priority(monkeypatch):
  from dataclasses import replace
  import cluster_renderer
  from cluster_live import standby_state
  from cluster_renderer import ClusterUiRenderer
  renderer = ClusterUiRenderer()
  state = replace(standby_state(), onroad=True, alert=None, driving_notices=('leadBrake',), lead_brake_sample=sample())
  clock = [10.]
  monkeypatch.setattr(cluster_renderer.time, 'monotonic', lambda: clock[0])
  for name in ('_rounded_rect', '_draw_text_with_stroke', '_draw_text'):
    monkeypatch.setattr(renderer, name, lambda *args, **kwargs: None)
  for name in ('begin_scissor_mode', 'end_scissor_mode', 'draw_circle_v'):
    monkeypatch.setattr(cluster_renderer.rl, name, lambda *args: None)
  assert not renderer._draw_driving_popup(state, 0)
  clock[0] = 10.31
  assert renderer._draw_driving_popup(state, 0)
  assert renderer._driving_notices.active == 'leadBrake'
  clock[0] = 10.4
  assert renderer._draw_driving_popup(replace(state, steering_warning=3), 0)
  assert renderer._popup_slide.update(10.4, warning=3).warning == 3
  clock[0] = 10.5
  renderer._draw_driving_popup(state, 0)
  assert renderer._driving_notices.active is None
