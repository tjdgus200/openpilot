"""Host-safe feedback, schema and warning contract checks; no hardware services."""

import ast
from enum import IntEnum
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from openpilot.cereal import car, log
from openpilot.selfdrive.controls.lib import longcontrol
from opendbc.car.hyundai.scc_decel import feedback_active, warning_active
from opendbc.car.hyundai.values import CAR, HyundaiFlags

ROOT = Path(__file__).resolve().parents[4]
T = 10_000_000_000


def cp(fingerprint=CAR.KIA_PV5):
  return NS(
    brand='hyundai',
    carFingerprint=fingerprint,
    notCar=False,
    flags=HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC,
    openpilotLongitudinalControl=True,
    longitudinalTuning=NS(kpBP=[0.0], kpV=[1.0], kiBP=[0.0], kiV=[0.0], kf=1.0),
    startingState=False,
    vEgoStarting=0.1,
    stopAccel=-2.0,
    stoppingDecelRate=0.8,
  )


def test_selected_output_tracking_preserves_PID_diagnostics(monkeypatch):
  monkeypatch.setattr(longcontrol, 'Params', lambda: NS(get_float=lambda key: -50.0))
  control = longcontrol.LongControl(cp())
  control.pid.update(0.1, feedforward=0.4)
  old = (control.pid.p, control.pid.i, control.pid.f)
  assert control.track_external_output(-2.24)
  assert control.last_output_accel == control.pid.control == -2.24
  assert (control.pid.p, control.pid.i, control.pid.f) == old
  assert control.pid.k_i == 0


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -4.01, 2.51])
def test_tracking_rejects_invalid_output_without_changing_state(monkeypatch, value):
  monkeypatch.setattr(longcontrol, 'Params', lambda: NS(get_float=lambda key: -50.0))
  control = longcontrol.LongControl(cp())
  control.last_output_accel = 0.5
  assert not control.track_external_output(value)
  assert control.last_output_accel == 0.5


def test_tracking_is_PV5_only(monkeypatch):
  monkeypatch.setattr(longcontrol, 'Params', lambda: NS(get_float=lambda key: -50.0))
  control = longcontrol.LongControl(cp(CAR.KIA_EV6))
  assert not control.track_external_output(-2.0)


def test_actuator_metadata_roundtrip_and_legacy_defaults():
  old_style = car.CarOutput.new_message(actuatorsOutput={'accel': 0.5})
  assert not old_style.actuatorsOutput.pv5SccDecelEnabled
  message = car.CarOutput.new_message(actuatorsOutput={'accel': -2.24, 'pv5SccDecelEnabled': True, 'pv5SccDecelActive': True, 'pv5SccDecelSourceMonoTime': T})
  with car.CarOutput.from_bytes(message.to_bytes()) as decoded:
    assert feedback_active(cp(), decoded.actuatorsOutput, T, T)
    cs = NS(canValid=True, canTimeout=False, brakePressed=False, gasPressed=False, gearShifter='drive', parkingBrake=False, brakeHoldActive=False)
    assert warning_active(cp(), cs, decoded.actuatorsOutput, T, T)
    cs.brakePressed = True
    assert not warning_active(cp(), cs, decoded.actuatorsOutput, T, T)


def test_exact_production_warning_definition_is_critical_and_warning_only():
  # Evaluate the actual source definition without importing messaging/audio workers.
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
  nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ('Priority', 'ET', 'Alert')]
  exec(compile(ast.Module(body=nodes, type_ignores=[]), 'production-alert-definitions', 'exec'), scope)
  definitions = (
    next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', None) == 'EVENTS')
    if any(isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', None) == 'EVENTS' for n in tree.body)
    else next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, 'id', None) == 'EVENTS' for t in n.targets))
  )
  node = next(v for k, v in zip(definitions.keys, definitions.values, strict=True) if isinstance(k, ast.Attribute) and k.attr == 'pv5SccDecel')
  event = eval(compile(ast.Expression(node), 'production-scc-alert', 'eval'), scope)
  assert set(event) == {'warning'}
  alert = event['warning']
  assert alert.alert_status == log.SelfdriveState.AlertStatus.critical
  assert alert.alert_size == log.SelfdriveState.AlertSize.full
  assert alert.priority < scope['Priority'].HIGHEST
  assert '!' in alert.alert_text_1 and 'Collision' not in alert.alert_text_2
  assert alert.audible_alert != car.CarControl.HUDControl.AudibleAlert.none
  assert alert.duration <= 1
  assert log.OnroadEvent.EventName.schema.enumerants['pv5SccDecel'] == 130


def test_setting_default_docs_and_warning_translation_contract():
  catalog = json.loads((ROOT / 'openpilot/selfdrive/carrot_settings.json').read_text())

  def mappings(value):
    if isinstance(value, dict):
      yield value
      for child in value.values():
        yield from mappings(child)
    elif isinstance(value, list):
      for child in value:
        yield from mappings(child)

  entry = next(v for v in mappings(catalog) if v.get('name') == 'Pv5SccDecelAssist')
  assert entry['control'] == 'toggle' and entry['default'] == 0 and entry['risk'] == 'high'
  assert all(entry.get(k) for k in ('title', 'etitle', 'ctitle', 'descr', 'edescr', 'cdescr'))
  menu = next(v for v in mappings(catalog) if v.get('id') == 'PV5_ASSIST')
  assert 'Pv5SccDecelAssist' in menu['params']
  assert '{"Pv5SccDecelAssist", {PERSISTENT, BOOL, "0"}}' in (ROOT / 'openpilot/common/params_keys.h').read_text()
  for lang in ('ko', 'en'):
    assert 'Pv5SccDecelAssist' in (ROOT / f'docs/user/{lang}/settings.md').read_text()
  translated = (ROOT / 'openpilot/selfdrive/ui/translations/app_ko.po').read_text()
  assert 'msgid "SCC BRAKING!"\nmsgstr "급감속 경고!"' in translated
