"""Bounded admission and driver-facing stage contract; no vehicle interfaces."""

import ast
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.cereal import car, log
from openpilot.common.pv5_cutin_assist import request_from_radar, constrain_accel, warning_stage, feedback_stage
from openpilot.selfdrive.carrot.radar_motion.cutin_assist import CutInAssistTracker
from openpilot.selfdrive.controls.lib.cutin_predecel import get_cutin_predecel_accel_limit
from opendbc.car.hyundai.values import CAR, HyundaiFlags

ROOT = Path(__file__).resolve().parents[4]
T = 10_000_000_000


def cp(**changes):
  values = {
    'brand': 'hyundai',
    'carFingerprint': CAR.KIA_PV5,
    'notCar': False,
    'flags': HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC,
    'openpilotLongitudinalControl': True,
  }
  return NS(**(values | changes))


def estimate(**changes):
  point = NS(source='frontRadar', track_id=60, measured=True, d_rel=9.0, y_rel=-2.3, v_rel=-3.0)
  values = {
    'point': point,
    'identity': ('frontRadar', 60, 1),
    'history_s': 1.0,
    'd_path': -2.3,
    'future_d_path': -0.5,
    'inward_rate': 0.6,
    'reported_inward_rate': 0.3,
    'inward_progress': 0.5,
    'recent_inward_progress': 0.15,
    'direction_consistency': 0.9,
    'recent_direction_consistency': 0.9,
    'lateral_net_fraction': 0.8,
    'recent_abs_yaw_max': 0.005,
    'confidence': 0.65,
    'time_to_overlap_s': 0.7,
    'unstable_fast_motion': False,
    'rear_pass': False,
    'parallel_drift': False,
    'passing_before_overlap': False,
    'entry_withdrawn': False,
    'stationary_pair_alias': False,
    'curve_alias': False,
    'close_front_supported': True,
    'jittering': False,
    'predecel_risk': False,
    'confirmed_cutin': False,
    'control_eligible': False,
  }
  return NS(**(values | changes))


def primed():
  tracker = CutInAssistTracker()
  assert tracker.update([estimate()], 10.0, 8.0) is None
  assert tracker.update([estimate()], 10.1, 8.0) is None
  assert tracker.update([estimate()], 10.2, 8.0).stage == 1
  return tracker


def test_caution_needs_continuous_evidence_and_promotes_without_extra_dwell():
  tracker = primed()
  assert tracker.update([estimate(predecel_risk=True)], 10.25, 8.0).stage == 2
  assert tracker.update([estimate()], 10.3, 8.0).stage == 1
  assert tracker.update([], 10.35, 8.0) is None
  assert tracker.update([estimate()], 10.4, 8.0) is None


def test_strong_existing_evidence_can_start_in_stage_two():
  assert CutInAssistTracker().update([estimate(confirmed_cutin=True, control_eligible=True)], 10.0, 8.0).stage == 2


@pytest.mark.parametrize(
  'field', ['unstable_fast_motion', 'rear_pass', 'parallel_drift', 'passing_before_overlap', 'entry_withdrawn', 'stationary_pair_alias', 'curve_alias']
)
def test_hard_veto_immediately_withdraws_both_stages(field):
  tracker = primed()
  assert tracker.update([estimate(**{field: True, 'predecel_risk': True})], 10.25, 8.0) is None


@pytest.mark.parametrize(
  'changes',
  [
    {'history_s': 0.1},
    {'inward_progress': 0.1},
    {'direction_consistency': 0.3},
    {'recent_direction_consistency': 0.1},
    {'lateral_net_fraction': 0.1},
    {'recent_abs_yaw_max': 0.2},
    {'time_to_overlap_s': None},
    {'time_to_overlap_s': 2.0},
    {'future_d_path': -2.4},
    {'inward_rate': 0.1},
    {'reported_inward_rate': 0.0},
    {'recent_inward_progress': 0.0},
    {'jittering': True},
    {'d_path': float('nan')},
    {'close_front_supported': False},
  ],
)
def test_no_single_optimistic_projection_or_incoherent_entry(changes):
  tracker = CutInAssistTracker()
  for t in (10.0, 10.1, 10.2):
    assert tracker.update([estimate(**changes)], t, 8.0) is None


def test_sensor_measurement_identity_and_clock_boundaries():
  for point in (
    NS(**(vars(estimate().point) | {'source': 'scc'})),
    NS(**(vars(estimate().point) | {'measured': False})),
    NS(**(vars(estimate().point) | {'v_rel': 0.1})),
    NS(**(vars(estimate().point) | {'d_rel': 100.0})),
  ):
    assert CutInAssistTracker().update([estimate(point=point, predecel_risk=True)], 10.0, 8.0) is None
  tracker = primed()
  assert tracker.update([estimate()], 10.2, 8.0) is None
  tracker = primed()
  assert tracker.update([estimate()], 10.4, 8.0) is None
  tracker = primed()
  assert tracker.update([estimate()], 10.25, 8.0, lane_change=True) is None
  tracker = primed()
  assert tracker.update([estimate(identity=('frontRadar', 60, 2))], 10.25, 8.0) is None


def test_mild_jitter_only_retains_already_coherent_caution_and_primary_retires_it():
  tracker = primed()
  assert tracker.update([estimate(jittering=True, lateral_net_fraction=0.4)], 10.25, 8.0).stage == 1
  assert tracker.update([estimate()], 10.3, 8.0, primary={'radar': True, 'radarTrackId': 60}) is None


def test_hysteresis_does_not_reacquire_weak_evidence_or_hide_withdrawal():
  weak = estimate(inward_progress=0.25, lateral_net_fraction=0.4, jittering=True)
  assert CutInAssistTracker().update([weak], 10.0, 8.0) is None
  tracker = primed()
  assert tracker.update([weak], 10.25, 8.0).stage == 1
  assert tracker.update([estimate(inward_progress=0.19)], 10.3, 8.0) is None
  assert tracker.update([weak], 10.35, 8.0) is None


def radar(stage=1, **changes):
  lead = NS(status=True, radar=True, dRel=9.0, vRel=-3.0, score=0.7)
  return NS(**({'pv5CutInAssistStage': stage, 'leadCutInAssist': lead, 'mdMonoTime': T, 'carStateMonoTime': T} | changes))


@pytest.mark.parametrize('stage', [0, 1, 2, 3])
def test_request_scope_and_stage(stage):
  request = request_from_radar(cp(), radar(stage), T)
  assert request.stage == (stage if stage in (1, 2) else 0)
  if request.stage:
    assert constrain_accel(0.5, request) <= 0
    assert constrain_accel(-2.0, request) == -2.0
  assert request_from_radar(cp(carFingerprint=CAR.KIA_EV6), radar(stage), T).stage == 0
  assert request_from_radar(cp(notCar=True), radar(stage), T).stage == 0
  assert request_from_radar(NS(brand='hyundai', carFingerprint='KIA_PV5', flags=0), radar(stage), T).stage == 0


@pytest.mark.parametrize('stamp', [0, T + 1, T - 150_000_001])
def test_expired_future_missing_sources_do_not_constrain(stamp):
  assert request_from_radar(cp(), radar(mdMonoTime=stamp), T).stage == 0
  assert request_from_radar(cp(), radar(carStateMonoTime=stamp), T).stage == 0


@pytest.mark.parametrize('distance', [3.0, 9.0, 20.0, 29.0])
@pytest.mark.parametrize('velocity', [-0.5, -2.0, -4.0])
def test_stage_two_matches_existing_ttc_ceiling(distance, velocity):
  state = radar(2)
  state.leadCutInAssist.dRel = distance
  state.leadCutInAssist.vRel = velocity
  state.leadCutInRisk = state.leadCutInAssist
  assert request_from_radar(cp(), state, T).limit == pytest.approx(get_cutin_predecel_accel_limit(state))


def output(stage=1, **changes):
  return NS(**({'pv5CutInAssistEnabled': True, 'pv5CutInAssistStage': stage, 'pv5CutInAssistSourceMonoTime': T, 'accel': 0.0} | changes))


def test_popup_requires_fresh_emitted_constraint_and_driver_admission():
  cs = NS(canValid=True, canTimeout=False, brakePressed=False, gasPressed=False, parkingBrake=False, brakeHoldActive=False, gearShifter='drive')
  assert warning_stage(cp(), cs, output(), T, T) == 1
  assert warning_stage(cp(), cs, output(2, accel=-0.4), T, T) == 2
  for field in ('brakePressed', 'gasPressed', 'parkingBrake', 'brakeHoldActive', 'canTimeout'):
    assert warning_stage(cp(), NS(**(vars(cs) | {field: True})), output(), T, T) == 0
  assert warning_stage(cp(), cs, output(), T, T + 150_000_001) == 0
  assert feedback_stage(cp(), output(accel=0.1), T, T) == 0
  assert feedback_stage(cp(), output(pv5CutInAssistEnabled=False), T, T) == 0


def production_alerts():
  tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/events.py').read_text())
  scope = {
    'IntEnum': IntEnum,
    'DT_CTRL': 0.01,
    'log': log,
    'car': car,
    'tr_noop': lambda text: text,
    'AlertStatus': log.SelfdriveState.AlertStatus,
    'AlertSize': log.SelfdriveState.AlertSize,
    'VisualAlert': car.CarControl.HUDControl.VisualAlert,
    'AudibleAlert': car.CarControl.HUDControl.AudibleAlert,
  }
  classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ('Priority', 'ET', 'Alert')]
  exec(compile(ast.Module(body=classes, type_ignores=[]), 'exact-alert-classes', 'exec'), scope)
  definitions = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', None) == 'EVENTS')
  result = {}
  for key, value in zip(definitions.keys, definitions.values, strict=True):
    if isinstance(key, ast.Attribute) and key.attr in ('pv5CutInCaution', 'pv5CutInPredecel', 'pv5SccDecel', 'driverDistracted1'):
      result[key.attr] = eval(compile(ast.Expression(value), 'exact-alert', 'eval'), scope)
  return result


def test_promotion_emphasis_and_all_driver_alerts_preempt_it():
  events = production_alerts()
  a = events['pv5CutInCaution']['warning']
  b = events['pv5CutInPredecel']['warning']
  assert set(events['pv5CutInCaution']) == set(events['pv5CutInPredecel']) == {'warning'}
  assert a.alert_size == log.SelfdriveState.AlertSize.small and b.alert_size == log.SelfdriveState.AlertSize.mid
  assert a.alert_status == log.SelfdriveState.AlertStatus.normal and b.alert_status == log.SelfdriveState.AlertStatus.userPrompt
  assert a.audible_alert == car.CarControl.HUDControl.AudibleAlert.none
  assert b.audible_alert == car.CarControl.HUDControl.AudibleAlert.prompt
  assert '!' in b.alert_text_1
  assert a.duration <= 5 and b.duration <= 5
  assert a.priority < b.priority < events['driverDistracted1']['permanent'].priority < events['pv5SccDecel']['warning'].priority


def test_actuator_stage_wire_defaults_and_roundtrip():
  assert car.CarOutput.new_message().actuatorsOutput.pv5CutInAssistStage == 0
  state = car.CarOutput.new_message(actuatorsOutput={'accel': -0.4, 'pv5CutInAssistEnabled': True, 'pv5CutInAssistStage': 2, 'pv5CutInAssistSourceMonoTime': T})
  with car.CarOutput.from_bytes(state.to_bytes()) as decoded:
    assert feedback_stage(cp(), decoded.actuatorsOutput, T, T) == 2
