"""Retired experimental stage must not survive in live control or settings."""
from pathlib import Path
import json

import pytest

from openpilot.cereal import car, log

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize('name', [
  'openpilot/common/pv5_cutin_assist.py',
  'openpilot/selfdrive/carrot/radar_motion/cutin_assist.py',
])
def test_staged_detector_and_request_module_removed(name):
  assert not (ROOT/name).exists()


@pytest.mark.parametrize('name', [
  'openpilot/selfdrive/carrot/radar/radard_dpath.py',
  'openpilot/selfdrive/carrot/radar_motion/controller.py',
  'openpilot/selfdrive/controls/controlsd.py',
  'openpilot/selfdrive/controls/lib/longitudinal_planner.py',
  'openpilot/selfdrive/selfdrived/events.py',
  'openpilot/selfdrive/selfdrived/selfdrived.py',
  'opendbc_repo/opendbc/car/hyundai/carcontroller.py',
  'opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py',
])
def test_no_live_stage_consumer_or_warning(name):
  source = (ROOT/name).read_text(encoding='utf-8')
  assert 'pv5CutIn' not in source and 'pv5_cutin_assist' not in source
  assert 'cutin_accel_ceiling' not in source


def test_setting_is_removed_even_if_an_old_saved_value_was_on():
  source = (ROOT/'openpilot/selfdrive/carrot_settings.json').read_text(encoding='utf-8')
  json.loads(source)
  assert 'Pv5CutInAssist' not in source
  keys = (ROOT/'openpilot/common/params_keys.h').read_text(encoding='utf-8')
  assert 'Pv5CutInAssist' not in keys
  assert 'Pv5SccDecelAssist' in source and 'Pv5SccDecelAssist' in keys


def test_retired_wire_ids_remain_readable_and_default_inactive():
  state = log.RadarState.new_message()
  assert not state.leadCutInAssist.status and state.pv5CutInAssistStage == 0
  output = car.CarOutput.new_message().actuatorsOutput
  assert not output.pv5CutInAssistEnabled
  assert output.pv5CutInAssistStage == output.pv5CutInAssistSourceMonoTime == 0
  assert log.OnroadEvent.EventName.pv5CutInCaution == 131
  assert log.OnroadEvent.EventName.pv5CutInPredecel == 132
