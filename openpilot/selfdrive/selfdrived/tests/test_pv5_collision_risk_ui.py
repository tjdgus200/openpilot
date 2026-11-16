"""Cruise-OFF presentation contracts; production methods with no audio/device I/O."""

import ast
import bisect
import copy
import json
from collections import defaultdict
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.cereal import car, log
from openpilot.selfdrive.selfdrived.tests.test_pv5_scc_warning import sound_engine, sound_state
from openpilot.system.pv5_incidents import runtime
from openpilot.system.pv5_incidents.priority import REPORTABLE_ALERTS, archive_reviewable, reportable

ROOT = Path(__file__).resolve().parents[4]
NAMES = ('pv5CollisionRisk', 'pv5CollisionRiskSilent')
SOUND = car.CarControl.HUDControl.AudibleAlert
EVENT = log.OnroadEvent.EventName


def production_ui():
  # Run the actual event/state/arbitration code while excluding unrelated device imports.
  tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/events.py').read_text(encoding='utf-8'))
  scope = {
    'bisect': bisect, 'copy': copy, 'IntEnum': IntEnum, 'log': log, 'car': car, 'DT_CTRL': 0.01,
    'tr': lambda text: text, 'tr_noop': lambda text: text,
    'AlertStatus': log.SelfdriveState.AlertStatus, 'AlertSize': log.SelfdriveState.AlertSize,
    'VisualAlert': car.CarControl.HUDControl.VisualAlert, 'AudibleAlert': SOUND,
    'EVENT_NAME': {v: k for k, v in EVENT.schema.enumerants.items()},
    'TEMP_STEER_EVENTS': (EVENT.steerTempUnavailable, EVENT.steerTempUnavailableSilent),
    'TEMP_STEER_WARNING_DELAY_FRAMES': 12, 'TEMP_STEER_WARNING_RESET_FRAMES': 100,
  }
  classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ('Priority', 'ET', 'Alert', 'Events', 'ImmediateDisableAlert')]
  exec(compile(ast.Module(body=classes, type_ignores=[]), 'production-event-classes', 'exec'), scope)
  definitions = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', None) == 'EVENTS')
  scope['EVENTS'] = {}
  selected = (*NAMES, 'fcw', 'aeb', 'stockAeb', 'controlsMismatch')
  for key, value in zip(definitions.keys, definitions.values, strict=True):
    if isinstance(key, ast.Attribute) and key.attr in selected:
      # AEB's no-entry alert is irrelevant to arbitration and imports other device UI.
      permanent = ast.Dict(keys=[k for k in value.keys if k.attr in ('PERMANENT', 'IMMEDIATE_DISABLE')],
                           values=[v for k, v in zip(value.keys, value.values, strict=True) if k.attr in ('PERMANENT', 'IMMEDIATE_DISABLE')])
      scope['EVENTS'][getattr(EVENT, key.attr)] = eval(compile(ast.fix_missing_locations(ast.Expression(permanent)), 'production-event-map', 'eval'), scope)
  scope['EmptyAlert'] = scope['Alert']('', '', log.SelfdriveState.AlertStatus.normal, log.SelfdriveState.AlertSize.none,
                                       scope['Priority'].LOWEST, car.CarControl.HUDControl.VisualAlert.none, SOUND.none, 0)
  state_tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/state.py').read_text(encoding='utf-8'))
  exec(compile(ast.Module(body=[n for n in state_tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))], type_ignores=[]),
               'production-state-machine', 'exec'), scope)
  scope.update(defaultdict=defaultdict, dataclass=dataclass)
  manager_tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/alertmanager.py').read_text(encoding='utf-8'))
  exec(compile(ast.Module(body=[n for n in manager_tree.body if isinstance(n, ast.ClassDef)], type_ignores=[]),
               'production-alert-manager', 'exec'), scope)
  return NS(**scope)


def create_alert(ui, name):
  events = ui.Events()
  events.add(getattr(EVENT, name))
  return events.create_alerts([ui.ET.PERMANENT, ui.ET.IMMEDIATE_DISABLE])[0]


def test_append_only_enums_and_trusted_schema_list():
  assert [EVENT.schema.enumerants[name] for name in (*NAMES, 'pv5SccDecelSilent')] == [135, 136, 134]
  known = json.loads((ROOT / 'openpilot/system/pv5_incidents/known_sources.json').read_text(encoding='utf-8'))
  assert set(known['alerts']) == set(EVENT.schema.enumerants)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('enabled', [False, True])
def test_warning_has_no_control_state_transition_and_is_visible_while_disabled(name, enabled):
  ui = production_ui()
  events = ui.Events()
  events.add(getattr(EVENT, name))
  assert set(ui.EVENTS[getattr(EVENT, name)]) == {ui.ET.PERMANENT}
  state = ui.StateMachine()
  state.state = log.SelfdriveState.OpenpilotState.enabled if enabled else log.SelfdriveState.OpenpilotState.disabled
  assert state.update(events) == (enabled, enabled)
  alerts = events.create_alerts(state.current_alert_types)
  assert len(alerts) == 1
  alert = alerts[0]
  assert alert.alert_type == name + '/permanent'
  assert alert.alert_status == log.SelfdriveState.AlertStatus.critical
  assert alert.priority == 4
  assert alert.visual_alert == car.CarControl.HUDControl.VisualAlert.none
  assert alert.audible_alert == (SOUND.prompt if name == NAMES[0] else SOUND.none)
  assert alert.duration == 1
  assert (alert.alert_text_1, alert.alert_text_2) == ('Collision risk', 'Brake if needed')
  message = events.to_msg()[0]
  assert message.permanent
  assert not any(getattr(message, event_type) for event_type in (
    'enable', 'preEnable', 'noEntry', 'userDisable', 'softDisable', 'immediateDisable', 'overrideLateral', 'overrideLongitudinal', 'warning'))
  manager = ui.AlertManager()
  manager.add_many(10, alerts)
  manager.process_alerts(10, {ui.ET.WARNING} if not enabled else set())
  assert manager.current_alert.alert_type == name + '/permanent'


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('higher', ['fcw', 'aeb', 'stockAeb', 'controlsMismatch'])
def test_existing_emergency_alerts_win_even_when_new_warning_starts_later(name, higher):
  ui = production_ui()
  manager = ui.AlertManager()
  emergency, risk = create_alert(ui, higher), create_alert(ui, name)
  manager.add_many(0, [emergency])
  manager.add_many(1, [risk])
  manager.process_alerts(1, set())
  assert emergency.priority == 5 and risk.priority == 4
  assert manager.current_alert.alert_type == emergency.alert_type


def test_visual_expires_without_refresh_and_silent_variant_replaces_pulse():
  ui = production_ui()
  manager = ui.AlertManager()
  pulse, silent = [create_alert(ui, name) for name in NAMES]
  manager.add_many(0, [pulse])
  manager.process_alerts(0, set())
  assert manager.current_alert.audible_alert == SOUND.prompt
  manager.add_many(1, [silent])
  manager.process_alerts(1, set())
  assert manager.current_alert.alert_type == NAMES[1] + '/permanent'
  manager.process_alerts(3, set())
  assert manager.current_alert is ui.EmptyAlert


def off_state(name, sound=None):
  sm = sound_state(name + '/permanent', sound)
  sm['selfdriveState'].enabled = False
  return sm


def test_off_cruise_one_shot_then_silent_and_new_episode():
  engine = sound_engine()
  pulse = off_state(NAMES[0], SOUND.prompt)
  engine.get_audible_alert(pulse)
  assert np.all(engine.get_sound_data(8) == 1.0)
  for _ in range(30):
    engine.get_audible_alert(pulse)
    assert not np.any(engine.get_sound_data(8))
  quiet = off_state(NAMES[1])
  quiet['carrotMan'].leftSec = 10
  engine.update_carrot_alert = lambda *_: pytest.fail('silent collision warning must not start navigation countdown')
  engine.get_audible_alert(quiet)
  assert engine.current_alert == SOUND.none
  assert not np.any(engine.get_sound_data(8))
  del engine.update_carrot_alert
  engine.get_audible_alert(pulse)
  assert np.all(engine.get_sound_data(8) == 1.0)


def test_scc_and_collision_pulse_edges_are_independent_for_same_waveform():
  engine = sound_engine()
  scc = sound_state('pv5SccDecel/warning', SOUND.prompt)
  off = off_state(NAMES[0], SOUND.prompt)
  for sm in (scc, off, scc, off):
    engine.get_audible_alert(sm)
    assert np.all(engine.get_sound_data(8) == 1.0)
    engine.get_audible_alert(sm)
    assert not np.any(engine.get_sound_data(8))


@pytest.mark.parametrize('kind', ['pv5CollisionRiskSilent/warning', 'pv5CollisionRisk/permanent', 'fcw/permanent', 'stockAeb/permanent'])
def test_only_exact_silent_risk_type_bypasses_critical_missing_sound_fallback(kind):
  engine = sound_engine()
  engine.get_audible_alert(sound_state(kind))
  assert engine.current_alert == SOUND.warningImmediate
  assert np.all(engine.get_sound_data(8) == 1.0)


def test_stale_silent_collision_does_not_hide_enabled_selfdrive_timeout():
  engine = sound_engine()
  sm = sound_state(NAMES[1] + '/permanent')
  sm.updated['selfdriveState'] = False
  sm.recv_time['selfdriveState'] = 90.0
  engine.get_audible_alert(sm)
  assert engine.current_alert == SOUND.warningImmediate


def test_clear_mid_pulse_finishes_one_waveform_without_looping():
  engine = sound_engine()
  engine.get_audible_alert(off_state(NAMES[0], SOUND.prompt))
  assert np.all(engine.get_sound_data(3) == 1.0)
  quiet = off_state(NAMES[1])
  engine.get_audible_alert(quiet)
  assert np.all(engine.get_sound_data(5) == 1.0)
  engine.get_audible_alert(quiet)
  assert not np.any(engine.get_sound_data(8))


@pytest.mark.parametrize('name', NAMES)
def test_new_presentation_does_not_wake_critical_error_ai(name):
  assert name not in REPORTABLE_ALERTS
  assert not reportable({'kind': 'system_alert', 'code': name})
  assert not archive_reviewable({'status': 'ok', 'error_codes': [name], 'candidates': []})
  assert reportable({'kind': 'system_alert', 'code': 'fcw'})
  assert archive_reviewable({'status': 'ok', 'error_codes': ['fcw'], 'candidates': []})


def test_existing_reporter_observer_ignores_new_permanent_events(monkeypatch):
  ui = production_ui()
  events = ui.Events()
  for name in NAMES:
    events.add(getattr(EVENT, name))
  monkeypatch.setattr(runtime, 'speed_proof', lambda sm: NS(pv5=True))
  monkeypatch.setattr(runtime, 'stopped', lambda *args: False)
  reporter = runtime.Reporter(None, None, NS(hostname='unused.invalid', port=None), b'')
  collected = []
  reporter.add = collected.append
  class SM(dict):
    valid = {'onroadEvents': True}
    alive = {'onroadEvents': True}
  sm = SM(onroadEvents=events.to_msg())
  reporter.observe(sm)
  assert collected == [] and reporter.previous == set()
  events.add(EVENT.fcw)
  sm['onroadEvents'] = events.to_msg()
  reporter.observe(sm)
  assert [row['code'] for row in collected] == ['fcw']


@pytest.mark.parametrize('language', ['ko', 'zh-CHS'])
def test_new_text_has_exact_translations(language):
  from openpilot.selfdrive.ui.translations.potools import parse_po
  _, entries = parse_po(ROOT / f'openpilot/selfdrive/ui/translations/app_{language}.po')
  catalog = {entry.msgid: entry for entry in entries}
  for text in ('Collision risk', 'Brake if needed'):
    assert catalog[text].msgstr


def production_collision_route(replay=False):
  tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/selfdrived.py').read_text(encoding='utf-8'))
  blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.Attribute)
            and n.test.attr == 'pv5_scc_supported' and any(isinstance(child, ast.Attribute) and child.attr == 'off_cruise_warning'
                                                        for child in ast.walk(n))]
  assert len(blocks) == 1
  function = ast.FunctionDef(
    name='route', args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=name) for name in ('self', 'CS', 'now_ns')],
                                    kwonlyargs=[], kw_defaults=[], defaults=[]), body=blocks, decorator_list=[])
  scope = {'EventName': EVENT, 'REPLAY': replay}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), 'production-off-cruise-routing', 'exec'), scope)
  return scope['route']


@pytest.mark.parametrize('supported,enabled,cruise,valid,expect', [
  (True, False, False, True, True), (False, False, False, True, False),
  (True, True, False, True, False), (True, False, True, True, False),
  (True, False, False, False, False),
])
def test_real_off_cruise_routing_adds_only_permanent_alert_without_enabling_controls(supported, enabled, cruise, valid, expect):
  from opendbc.car.hyundai.collision_warning import OffCruiseWarning, RiskInputs, write_inputs
  ui = production_ui()
  t = 10_000_000_000
  cs = car.CarState.new_message(vEgo=15.0, vEgoRaw=15.0, gearShifter='drive', canValid=True)
  cs.cruiseState.enabled = cruise
  write_inputs(cs, RiskInputs(source_valid=True, stamp=t, raw=-3.2, distance=15.0, relative_speed=-10.0, target_valid=True))
  before = cs.to_dict()
  owner = NS(pv5_scc_supported=supported, enabled=enabled, active=enabled, car_state_valid=valid,
             car_state_mono_time=t, off_cruise_warning=OffCruiseWarning(), events=ui.Events())
  production_collision_route()(owner, cs, t)
  assert cs.to_dict() == before
  assert (owner.enabled, owner.active) == (enabled, enabled)
  assert owner.events.names == ([EVENT.pv5CollisionRisk] if expect else [])
  machine = ui.StateMachine()
  machine.state = log.SelfdriveState.OpenpilotState.enabled if enabled else log.SelfdriveState.OpenpilotState.disabled
  assert machine.update(owner.events) == (enabled, enabled)
  alerts = owner.events.create_alerts(machine.current_alert_types)
  assert len(alerts) == int(expect)
  if expect:
    assert alerts[0].event_type == ui.ET.PERMANENT
    for frame in range(1, 21):
      stamp = t + frame * 10_000_000
      cs.pv5CollisionWarning.sourceMonoTime = stamp
      owner.car_state_mono_time = stamp
      owner.events.clear()
      production_collision_route()(owner, cs, stamp)
      assert owner.events.names == [EVENT.pv5CollisionRisk if frame < 15 else EVENT.pv5CollisionRiskSilent]
      assert machine.update(owner.events) == (False, False)
    owner.car_state_valid = False
    owner.events.clear()
    production_collision_route()(owner, cs, t + 210_000_000)
    assert owner.events.names == []


@pytest.mark.parametrize('angle_limit,expected_type', [(False, 'pv5SteeringAttention'), (True, 'pv5SteeringAngleLimit')])
def test_silent_collision_does_not_suppress_existing_steering_attention(angle_limit, expected_type):
  engine = sound_engine()
  engine.get_audible_alert(off_state(NAMES[1]), steering_attention=True, steering_limit=angle_limit)
  assert engine.current_alert == SOUND.prompt
  assert engine.current_alert_type == expected_type
  assert np.all(engine.get_sound_data(8) == 1.0)
  engine.get_audible_alert(off_state(NAMES[1]), steering_attention=True, steering_limit=angle_limit)
  assert not np.any(engine.get_sound_data(8))


@pytest.mark.parametrize('replay,expected', [(True, True), (False, False)], ids=['replay_uses_exact_integer_time', 'live_rejects_future_timestamp'])
def test_routing_avoids_replay_float_roundtrip_without_weakening_live_freshness(replay, expected):
  from opendbc.car.hyundai.collision_warning import OffCruiseWarning, RiskInputs, write_inputs
  ui = production_ui()
  t = 10_000_000_001
  cs = car.CarState.new_message(vEgo=15.0, vEgoRaw=15.0, gearShifter='drive', canValid=True)
  write_inputs(cs, RiskInputs(source_valid=True, stamp=t, raw=-3.2, distance=15.0, relative_speed=-10.0, target_valid=True))
  owner = NS(pv5_scc_supported=True, enabled=False, active=False, car_state_valid=True,
             car_state_mono_time=t, off_cruise_warning=OffCruiseWarning(), events=ui.Events())
  # Simulate the earlier float-seconds round trip losing one nanosecond.
  production_collision_route(replay=replay)(owner, cs, t - 1)
  assert owner.events.names == ([EVENT.pv5CollisionRisk] if expected else [])
  assert (owner.enabled, owner.active) == (False, False)
