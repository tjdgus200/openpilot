"""Qualified warning-only metadata and bounded nuisance reduction; no hardware."""

import ast
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.cereal import car, log
from openpilot.common.pv5_scc_warning import SccWarning, capture_inputs, write_inputs
from opendbc.car.hyundai.tests.test_scc_decel import cp, primed_source

ROOT = Path(__file__).resolve().parents[4]
T = 10_000_000_000


def state(**changes):
  values = {
    'canValid': True,
    'canTimeout': False,
    'brakePressed': False,
    'gasPressed': False,
    'parkingBrake': False,
    'brakeHoldActive': False,
    'gearShifter': 'drive',
    'vEgo': 10.0,
  }
  return NS(**(values | changes))


def output(t=T, **changes):
  values = {
    'pv5SccDecelEnabled': True,
    'pv5SccDecelActive': True,
    'pv5SccDecelSourceMonoTime': t,
    'accel': -2.1,
    'pv5SccWarningVersion': 1,
    'pv5SccWarningMonoTime': t,
    'pv5SccWarningRaw': -2.1,
    'pv5SccWarningBaseAccel': -1.9,
    'pv5SccWarningDistance': 40.0,
    'pv5SccWarningRelSpeed': -1.0,
    'pv5SccWarningTargetValid': True,
  }
  return NS(**(values | changes))


def cue(tracker, seconds=0.0, cs=None, **changes):
  t = T + round(seconds * 1e9)
  return tracker.update(cp(), cs or state(), output(t, **changes), t, t)


def test_ordinary_scc_assistance_is_small_and_silent():
  result = cue(SccWarning())
  assert result.level == 1 and not result.audible


@pytest.mark.parametrize(
  'changes',
  [
    {'pv5SccWarningDistance': 8.0, 'pv5SccWarningRelSpeed': -1.0},
    {'pv5SccWarningDistance': 25.0, 'pv5SccWarningRelSpeed': -15.0},
    {'pv5SccWarningTargetValid': False, 'pv5SccWarningRaw': -3.1, 'accel': -3.1},
    {'pv5SccWarningDistance': 19.4, 'pv5SccWarningRelSpeed': 0.7, 'pv5SccWarningBaseAccel': 0.5},
  ],
)
def test_near_or_short_ttc_or_severe_or_disagreement_is_urgent(changes):
  result = cue(SccWarning(), **changes)
  assert result.level >= 2 and result.audible


def test_close_but_not_approaching_does_not_alone_cause_red():
  assert cue(SccWarning(), pv5SccWarningDistance=5.0, pv5SccWarningRelSpeed=0.2).level == 1


def test_stationary_estimate_needs_coherent_fresh_samples_but_not_ten_meters():
  tracker = SccWarning()
  for i in range(10):
    result = cue(tracker, i * 0.02, cs=state(vEgo=15.0), pv5SccWarningDistance=40.0 - i * 0.3, pv5SccWarningRelSpeed=-15.0)
    assert result.level == (1 if i < 8 else 2)


def test_stationary_estimate_resets_on_target_jump_and_outward_motion():
  tracker = SccWarning()
  for i in range(5):
    assert cue(tracker, i * 0.02, cs=state(vEgo=15.0), pv5SccWarningDistance=40.0 - i * 0.3, pv5SccWarningRelSpeed=-15.0).level == 1
  assert cue(tracker, 0.1, cs=state(vEgo=15.0), pv5SccWarningDistance=50.0, pv5SccWarningRelSpeed=-15.0).level == 1
  assert cue(tracker, 0.2, pv5SccWarningDistance=45.0, pv5SccWarningRelSpeed=1.0).level == 1


def test_same_danger_has_one_pulse_and_more_urgent_danger_realerts():
  tracker = SccWarning()
  assert cue(tracker, pv5SccWarningDistance=8.0).audible
  assert cue(tracker, 0.1, pv5SccWarningDistance=8.0).audible
  assert not cue(tracker, 0.2, pv5SccWarningDistance=8.0).audible
  result = cue(tracker, 0.22, pv5SccWarningDistance=5.0, pv5SccWarningRelSpeed=-10.0)
  assert result.level == 3 and result.audible
  assert not cue(tracker, 0.4, pv5SccWarningDistance=5.0, pv5SccWarningRelSpeed=-10.0).audible


def test_short_clear_gap_does_not_repeat_but_new_risk_after_clear_does():
  tracker = SccWarning()
  assert cue(tracker, pv5SccWarningDistance=8.0).audible
  assert cue(tracker, 0.2).level == 2  # brief visual hold, without repeating audio
  assert not cue(tracker, 0.3, pv5SccWarningDistance=8.0).audible
  for i in range(4, 17):
    assert cue(tracker, i * 0.1).level == (2 if i < 6 else 1)
  assert cue(tracker, 1.7, pv5SccWarningDistance=8.0).audible


@pytest.mark.parametrize(
  'changes',
  [
    {'pv5SccWarningVersion': 0},
    {'pv5SccWarningMonoTime': 0},
    {'pv5SccWarningRaw': float('nan')},
    {'pv5SccWarningBaseAccel': float('inf')},
  ],
)
def test_unknown_metadata_retains_qualified_urgent_warning(changes):
  assert cue(SccWarning(), **changes).level == 2


@pytest.mark.parametrize(
  'changes',
  [
    {'brakePressed': True},
    {'gasPressed': True},
    {'canValid': False},
    {'canTimeout': True},
    {'parkingBrake': True},
    {'brakeHoldActive': True},
    {'gearShifter': 'park'},
  ],
)
def test_driver_and_invalid_vehicle_conditions_clear_warning(changes):
  assert cue(SccWarning(), cs=state(**changes), pv5SccWarningRaw=-3.5).level == 0


def test_expired_output_and_unsupported_car_never_create_warning():
  tracker = SccWarning()
  assert tracker.update(cp(), state(), output(), T, T + 100_000_001).level == 0
  assert tracker.update(NS(**(vars(cp()) | {'notCar': True})), state(), output(), T, T).level == 0
  assert tracker.update(cp(), state(), output(), T, T, enabled=False).level == 0


@pytest.mark.parametrize('distance,relative_speed,expected', [(10.0, -0.5, 2), (10.01, -0.5, 1), (20.0, -10.0, 2), (20.1, -10.0, 1)])
def test_close_distance_and_ttc_trial_boundaries(distance, relative_speed, expected):
  assert cue(SccWarning(), pv5SccWarningDistance=distance, pv5SccWarningRelSpeed=relative_speed).level == expected


def test_visual_hold_cannot_outlive_fresh_or_driver_admission():
  tracker = SccWarning()
  assert cue(tracker, pv5SccWarningDistance=8.0).level == 2
  assert cue(tracker, 0.2).level == 2
  assert cue(tracker, 0.26).level == 1
  assert cue(tracker, 0.3, pv5SccWarningDistance=8.0).level == 2
  assert cue(tracker, 0.31, cs=state(brakePressed=True)).level == 0


def test_brief_invalid_output_hides_display_without_rearming_same_risk_audio():
  tracker = SccWarning()
  assert cue(tracker, pv5SccWarningDistance=8.0).audible
  t = T + 200_000_000
  assert tracker.update(cp(), state(), output(t), t, t, output_valid=False).level == 0
  assert not cue(tracker, 0.25, pv5SccWarningDistance=8.0).audible


def test_only_already_qualified_original_packet_can_supply_metadata():
  _, parser, source = primed_source()
  parser.vl['SCC_CONTROL'].update(ACC_ObjDist=19.4, ACC_ObjRelSpd=0.7, HUD_LEAD_INFO=2)
  inputs = capture_inputs(parser, source.observation, 0.5)
  assert inputs.target_valid and inputs.distance == 19.4 and inputs.raw == source.observation.raw
  out = car.CarOutput.new_message().actuatorsOutput
  write_inputs(out, inputs)
  assert out.pv5SccWarningMonoTime == source.observation.stamp
  parser.ts_nanos['SCC_CONTROL']['COUNTER'] += 1
  assert capture_inputs(parser, source.observation, 0.5).stamp == 0


def test_bad_optional_target_data_cannot_throw_into_actuation():
  _, parser, source = primed_source()
  parser.vl['SCC_CONTROL'].update(ACC_ObjDist=None, ACC_ObjRelSpd=0, HUD_LEAD_INFO=2)
  inputs = capture_inputs(parser, source.observation, 0.5)
  assert not inputs.target_valid and inputs.stamp == source.observation.stamp


def production_alerts():
  tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/events.py').read_text(encoding='utf-8'))
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
  exec(compile(ast.Module(body=classes, type_ignores=[]), 'production-alert-classes', 'exec'), scope)
  definitions = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', None) == 'EVENTS')
  result = {}
  for key, value in zip(definitions.keys, definitions.values, strict=True):
    if isinstance(key, ast.Attribute) and key.attr in ('pv5SccDecel', 'pv5SccDecelSilent', 'pv5SccDecelInfo', 'fcw', 'driverDistracted1'):
      result[key.attr] = eval(compile(ast.Expression(value), 'production-alert', 'eval'), scope)
  return result


def test_alert_levels_are_warning_only_and_do_not_mask_other_safety_alerts():
  events = production_alerts()
  a, b, c = [events[k]['warning'] for k in ('pv5SccDecelInfo', 'pv5SccDecel', 'pv5SccDecelSilent')]
  assert a.alert_size == log.SelfdriveState.AlertSize.small
  assert a.audible_alert == c.audible_alert == car.CarControl.HUDControl.AudibleAlert.none
  assert b.audible_alert == car.CarControl.HUDControl.AudibleAlert.prompt
  assert b.alert_status == c.alert_status == log.SelfdriveState.AlertStatus.critical
  assert b.alert_size == c.alert_size == log.SelfdriveState.AlertSize.full
  assert a.priority < events['driverDistracted1']['permanent'].priority
  assert b.priority < events['fcw']['permanent'].priority
  for key in ('pv5SccDecelInfo', 'pv5SccDecel', 'pv5SccDecelSilent'):
    assert set(events[key]) == {'warning'}


def sound_engine():
  source = ast.parse((ROOT / 'openpilot/selfdrive/ui/soundd.py').read_text(encoding='utf-8'))
  definition = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'Soundd')
  selected = [
    n
    for n in definition.body
    if isinstance(n, ast.Assign) or isinstance(n, ast.FunctionDef) and n.name in ('get_sound_data', 'update_alert', 'get_audible_alert', 'update_carrot_alert')
  ]
  functions = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in ('check_selfdrive_timeout_alert', 'dm_warning_volume')]
  alerts = car.CarControl.HUDControl.AudibleAlert
  scope = {
    'np': np,
    'car': car,
    'log': log,
    'AudibleAlert': alerts,
    'MAX_VOLUME': 1.0,
    'SELFDRIVE_STATE_TIMEOUT': 5.0,
    'time': NS(monotonic=lambda: 100.0),
    'cloudlog': NS(error=lambda *a: None),
    'sound_list': {alerts.prompt: ('prompt.wav', 1, 1.0), alerts.warningImmediate: ('warning.wav', None, 1.0)},
  }
  cls = ast.ClassDef(name='Soundd', bases=[], keywords=[], body=selected, decorator_list=[])
  exec(compile(ast.fix_missing_locations(ast.Module(body=[*functions, cls], type_ignores=[])), 'production-sound-methods', 'exec'), scope)
  engine = scope['Soundd']()
  engine.current_alert = alerts.none
  engine.current_alert_type = ''
  engine.current_sound_frame = 0
  engine.current_volume = 1.0
  engine.carrot_count_down = 0
  engine.loaded_sounds = {a: np.full(8, 0.5, dtype=np.float32) for a in (alerts.prompt, alerts.warningImmediate)}
  engine.critical_gains = dict.fromkeys(engine.loaded_sounds, 2.0)
  return engine


def sound_state(kind, sound=None):
  alerts = car.CarControl.HUDControl.AudibleAlert

  class SM:
    updated = {'selfdriveState': True, 'carrotMan': True}
    recv_time = {'selfdriveState': 100.0}
    data = {
      'selfdriveState': NS(
        alertType=kind, alertStatus=log.SelfdriveState.AlertStatus.critical, alertSound=NS(raw=alerts.none if sound is None else sound), enabled=True
      ),
      'carrotMan': NS(leftSec=0),
    }

    def __getitem__(self, key):
      return self.data[key]

  return SM()


def test_urgent_sound_plays_once_then_red_can_stay_silent():
  engine = sound_engine()
  pulse = sound_state('pv5SccDecel/warning', car.CarControl.HUDControl.AudibleAlert.prompt)
  engine.get_audible_alert(pulse)
  assert np.all(engine.get_sound_data(8) == 1.0)
  for _ in range(20):
    engine.get_audible_alert(pulse)
    assert not np.any(engine.get_sound_data(8))
  quiet = sound_state('pv5SccDecelSilent/warning')
  quiet['carrotMan'].leftSec = 10  # No unrelated navigation sound during this red state.
  engine.get_audible_alert(quiet)
  assert not np.any(engine.get_sound_data(8))
  engine.get_audible_alert(pulse)
  assert np.all(engine.get_sound_data(8) == 1.0)


@pytest.mark.parametrize('kind', ['fcw/permanent', 'driverUnresponsive3/permanent', 'pv5SccDecelSilent/noEntry'])
def test_other_critical_sounds_keep_the_existing_fallback(kind):
  engine = sound_engine()
  engine.get_audible_alert(sound_state(kind))
  assert engine.current_alert == car.CarControl.HUDControl.AudibleAlert.warningImmediate
  assert np.all(engine.get_sound_data(8) == 1.0)


def test_stale_silent_SCC_does_not_disable_selfdrive_timeout_sound():
  engine = sound_engine()
  sm = sound_state('pv5SccDecelSilent/warning')
  sm.updated['selfdriveState'] = False
  sm.recv_time['selfdriveState'] = 90.0
  engine.get_audible_alert(sm)
  assert engine.current_alert == car.CarControl.HUDControl.AudibleAlert.warningImmediate


def test_twenty_hz_sound_poll_gets_one_play_per_episode_and_escalation():
  engine, tracker = sound_engine(), SccWarning()
  plays = 0
  for i in range(161):
    seconds = i * 0.05
    if 1.0 <= seconds < 4.0:
      changes = {'pv5SccWarningDistance': 8.0}
    elif 4.0 <= seconds < 5.0:
      changes = {'pv5SccWarningDistance': 5.0, 'pv5SccWarningRelSpeed': -10.0}
    elif 7.0 <= seconds:
      changes = {'pv5SccWarningDistance': 8.0}
    else:
      changes = {}
    result = cue(tracker, seconds, **changes)
    name = 'pv5SccDecelInfo' if result.level == 1 else 'pv5SccDecel' if result.audible else 'pv5SccDecelSilent'
    sm = sound_state(name + '/warning', car.CarControl.HUDControl.AudibleAlert.prompt if result.audible else None)
    if result.level == 1:
      sm['selfdriveState'].alertStatus = log.SelfdriveState.AlertStatus.normal
    engine.get_audible_alert(sm)
    plays += bool(np.any(engine.get_sound_data(8)))
  assert plays == 3


def test_actual_selfdrived_routing_uses_current_output_and_single_warning_variant():
  source = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/selfdrived.py').read_text(encoding='utf-8'))
  block = next(n for n in ast.walk(source) if isinstance(n, ast.If) and isinstance(n.test, ast.Attribute) and n.test.attr == 'pv5_scc_supported')
  function = ast.FunctionDef(
    name='route',
    args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='self'), ast.arg(arg='CS')], kwonlyargs=[], kw_defaults=[], defaults=[]),
    body=[block],
    decorator_list=[],
  )
  now = [T]
  scope = {'REPLAY': False, 'time': NS(monotonic_ns=lambda: now[0]), 'SccWarning': SccWarning, 'EventName': log.OnroadEvent.EventName}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), 'production-warning-routing', 'exec'), scope)

  class SM(dict):
    valid = {'carOutput': True}
    logMonoTime = {'carOutput': T}

  sm = SM()
  received = []
  owner = NS(pv5_scc_supported=True, scc_warning=None, enabled=True, CP=cp(), sm=sm, events=NS(add=received.append))

  def step(seconds, raw=-2.1, valid=True, enabled=True):
    now[0] = T + round(seconds * 1e9)
    sm.logMonoTime['carOutput'] = now[0]
    sm.valid['carOutput'] = valid
    owner.enabled = enabled
    message = car.CarOutput.new_message()
    for key, value in vars(output(now[0], pv5SccWarningRaw=raw)).items():
      setattr(message.actuatorsOutput, key, value)
    sm['carOutput'] = message
    received.clear()
    scope['route'](owner, state())
    return received.copy()

  assert step(0.0) == [log.OnroadEvent.EventName.pv5SccDecelInfo]
  assert step(0.02, -3.1) == [log.OnroadEvent.EventName.pv5SccDecel]
  assert step(0.2, -3.1) == [log.OnroadEvent.EventName.pv5SccDecelSilent]
  assert step(0.22, -3.1, valid=False) == []
  assert step(0.24, -3.1) == [log.OnroadEvent.EventName.pv5SccDecelSilent]
  assert step(0.26, -3.1, enabled=False) == []
