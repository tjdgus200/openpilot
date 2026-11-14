"""Notification-only PV5 policy, using actual production alert definitions."""

import ast
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace as NS

from openpilot.cereal import car, log
from openpilot.selfdrive.controls.lib.cutin_alert import CutinAlertCandidate, CutinAlertTracker, promoted_cutin_candidates

ROOT = Path(__file__).resolve().parents[4]


def alerts():
  tree = ast.parse((ROOT / 'openpilot/selfdrive/selfdrived/events.py').read_text(encoding='utf-8'))
  scope = {
    'IntEnum': IntEnum,
    'DT_CTRL': 0.01,
    'car': car,
    'log': log,
    'tr_noop': lambda text: text,
    'messaging': NS(SubMaster=object),
    'AlertStatus': log.SelfdriveState.AlertStatus,
    'AlertSize': log.SelfdriveState.AlertSize,
    'VisualAlert': car.CarControl.HUDControl.VisualAlert,
    'AudibleAlert': car.CarControl.HUDControl.AudibleAlert,
  }
  selected = [
    n
    for n in tree.body
    if isinstance(n, ast.ClassDef)
    and n.name in ('Priority', 'ET', 'Alert', 'EngagementAlert')
    or isinstance(n, ast.FunctionDef)
    and n.name == 'radar_cutin_alert'
  ]
  exec(compile(ast.Module(body=selected, type_ignores=[]), 'actual-cut-in-alert', 'exec'), scope)
  return scope


def test_confirmed_cutin_is_silent_low_priority_and_bounded_in_pv5_only():
  scope = alerts()
  for name, not_car, expected in [('KIA_PV5', False, 'none'), ('KIA_PV5', True, 'radarCutin'), ('OTHER', False, 'radarCutin')]:
    alert = scope['radar_cutin_alert'](NS(carFingerprint=name, notCar=not_car), None, None, False, 0, None)
    assert alert.audible_alert == getattr(car.CarControl.HUDControl.AudibleAlert, expected)
    if expected == 'none':
      assert alert.alert_text_1 == 'Vehicle cut-in detected' and alert.alert_text_2 == ''
      assert alert.alert_size == log.SelfdriveState.AlertSize.small
      assert alert.alert_status == log.SelfdriveState.AlertStatus.normal
      assert alert.priority < scope['Priority'].LOW
      assert alert.duration == 150
    else:
      assert alert.alert_size == log.SelfdriveState.AlertSize.none


def test_display_trigger_still_requires_promoted_radar_candidate():
  item = CutinAlertCandidate(60, 9.8, -0.13, -1.2)
  tracker = CutinAlertTracker()
  assert not tracker.update(promoted_cutin_candidates((item,), None))
  assert tracker.update(promoted_cutin_candidates((item,), item))
  assert not tracker.update(promoted_cutin_candidates((item,), item))
  # Continuous physical-object re-ID stays deduplicated by the existing tracker.
  moved = CutinAlertCandidate(61, 9.6, -0.1, -1.1)
  assert not tracker.update(promoted_cutin_candidates((moved,), moved))


def test_both_pv5_popup_consumers_filter_legacy_cutin_statuses_only():
  for path in ('ui/onroad/pv5_driving_feedback.py', 'carrot/cluster/cluster_live.py'):
    tree = ast.parse((ROOT / 'openpilot/selfdrive' / path).read_text(encoding='utf-8'))
    generator = next(
      n
      for n in ast.walk(tree)
      if isinstance(n, ast.GeneratorExp)
      and isinstance(n.generators[0].iter, ast.Call)
      and getattr(n.generators[0].iter.func, 'id', '') == 'driving_notice_events'
    )
    generator.generators[0].iter = ast.Name(id='events', ctx=ast.Load())
    expression = ast.fix_missing_locations(ast.Expression(generator))
    assert tuple(eval(compile(expression, path, 'eval'), {'events': ('cutin', 'cutinTrack', 'cutinBrake', 'laneChange', 'leadBrake', 'curve')})) == (
      'laneChange',
      'leadBrake',
      'curve',
    )


def test_other_alert_definitions_are_unchanged_in_scope():
  from openpilot.selfdrive.selfdrived.tests.test_pv5_scc_warning import production_alerts

  events = production_alerts()
  assert events['driverDistracted1']['permanent'].priority > alerts()['Priority'].LOWER
  assert events['pv5SccDecel']['warning'].audible_alert == car.CarControl.HUDControl.AudibleAlert.prompt
  assert events['fcw']['permanent'].audible_alert == car.CarControl.HUDControl.AudibleAlert.stopStop
