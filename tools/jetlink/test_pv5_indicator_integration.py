"""PV5 admission guards remain shared by the new model observer and sender."""
import json
from pathlib import Path

import pytest

from blink_observation import allowed
from openpilot.selfdrive.controls.lib.desire_lib.indicator_observer import ModelIndicator
from test_indicator_reconstruction import scene, run


@pytest.mark.parametrize('fault', ['yellow', 'unknown', 'outer', 'width', 'edge_uncertainty'])
def test_new_path_and_sender_require_positive_adjacent_lane(fault):
  cs, md = scene()
  if fault == 'yellow':
    cs.rightLaneLine = 21
  elif fault == 'unknown':
    cs.rightLaneLine = -1
  elif fault == 'outer':
    md.laneLineProbs[3] = .1
  elif fault == 'width':
    md.laneLines[3].y = [3.] * 13
  elif fault == 'edge_uncertainty':
    md.roadEdgeStds[1] = float('nan')
  detector = ModelIndicator()
  run(detector, cs, md)
  assert detector.active is None
  md.meta.observedIndicator = 'right'
  md.meta.laneChangeState = 'off'
  from types import SimpleNamespace
  assert not allowed(cs, SimpleNamespace(latActive=True), md, 'model_path', 10.45)


def test_new_options_are_off_in_both_settings_and_native_defaults():
  root = Path(__file__).resolve().parents[2]
  settings = json.loads((root/'openpilot/selfdrive/carrot_settings.json').read_text(encoding='utf8'))
  keys = (root/'openpilot/common/params_keys.h').read_text(encoding='utf8')
  for name in ('ManualLaneChangeIndicator', 'ModelDesireIndicator', 'ModelPathIndicator'):
    setting = next(s for s in settings['params'] if s['name'] == name)
    assert setting['default'] == 0
    assert '{"' + name + '", {PERSISTENT, BOOL, "0"}}' in keys
