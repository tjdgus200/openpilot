"""Source-side loss evidence and telemetry-only model publisher integration."""
import ast
from pathlib import Path

import pytest

from openpilot.common.model_frame_drops import ModelFrameDropWindow, SECOND


def test_missing_output_frames_are_counted_once_and_reported_at_one_hz():
  counter = ModelFrameDropWindow()
  start = 100 * SECOND
  for frame in range(41):
    if frame in (4, 5, 13):
      continue
    result = counter.update(1000 + frame, start + frame * 50_000_000)
    if frame < 20:
      assert result is None
    elif frame < 40:
      assert result == (3, start + SECOND)
    else:
      assert result == (0, start + 2 * SECOND)


@pytest.mark.parametrize('frame,stamp', [(1020, 102 * SECOND), (2, 101_100_000_000),
                                       (1010, 101_100_000_000), (1022, 100 * SECOND),
                                       (0xffffffff, 102 * SECOND), (-1, 101_100_000_000),
                                       (True, 101_100_000_000), (1022, 0)])
def test_start_stall_reset_duplicate_invalid_and_clock_reversal_are_unknown(frame, stamp):
  counter = ModelFrameDropWindow()
  for n in range(21):
    counter.update(1000+n, 100*SECOND+n*50_000_000)
  assert counter.completed == (0, 101*SECOND)
  assert counter.update(frame, stamp) is None
  assert counter.completed is None and not counter.gaps and counter.count == 0


def test_gap_exactly_at_window_end_is_included_and_history_is_bounded():
  counter = ModelFrameDropWindow()
  for n in range(20):
    counter.update(n, 100*SECOND+n*50_000_000)
  assert counter.update(23, 101*SECOND) == (3, 101*SECOND)
  # No unbounded history, even if a corrupt source claims >64 gap events/ms.
  for n in range(1, 66):
    counter.update(23+2*n, 101*SECOND+n*1000)
  assert len(counter.gaps) <= 64


@pytest.mark.parametrize('seed', range(10))
def test_rolling_count_matches_offline_event_history(seed):
  import random
  rng = random.Random(seed)
  counter = ModelFrameDropWindow()
  gaps, previous, last_report = [], None, None
  for n in range(2000):
    if rng.random() < .15:
      continue
    stamp = 100*SECOND + n*50_000_000
    result = counter.update(n, stamp)
    if previous is not None and n-previous > 1:
      gaps.append((stamp, n-previous-1))
    if result is not None and result != last_report:
      assert result[1] == stamp
      assert result[0] == sum(count for at, count in gaps if 0 <= stamp-at < SECOND)
      if last_report is not None:
        assert result[1] - last_report[1] >= SECOND
      last_report = result
    assert len(counter.gaps) <= 20
    previous = n


def test_publish_state_only_adds_metadata_and_uses_existing_id_timestamp():
  from types import SimpleNamespace
  from openpilot.selfdrive.modeld.fill_model_msg import PublishState, fill_model_msg
  from openpilot.selfdrive.modeld.parse_model_outputs import Parser
  from openpilot.cereal import log
  import numpy as np
  from openpilot.common import big_model_catalog as catalog
  from jetlink.spec import ModelSpec
  from openpilot.selfdrive.modeld.jetlink.contracts import parse_outputs

  for name in catalog.available():
    spec = ModelSpec.from_dict(catalog.model_spec(name))
    parsed = parse_outputs(Parser(), spec, np.zeros(spec.output_nelem, dtype=np.float32))
    state = PublishState()
    reference = PublishState()
    reference.frame_drops = SimpleNamespace(update=lambda *_: None)
    for n in (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20):
      event = log.Event.new_message(modelV2={})
      action = log.ModelDataV2.Action.new_message(desiredAcceleration=0., desiredCurvature=0.)
      fill_model_msg(event, parsed, action, state, n, n, n, .25,
                     100*SECOND+n*50_000_000, .04, True)
      data = event.modelV2
      assert data.frameId == n and data.modelExecutionTime == pytest.approx(.04)
      assert data.frameDropPerc == 25. and data.frameDropStats.valid == (n >= 20)
      # All other model fields are invariant for fixed synthetic neural output.
      fields = data.to_dict()
      for field in ('frameId', 'frameIdExtra', 'timestampEof', 'frameDropStats'):
        fields.pop(field)
      baseline = log.Event.new_message(modelV2={})
      # Disable only the display counter; all other publisher/history work runs.
      fill_model_msg(baseline, parsed, action, reference, n, n, n, .25,
                     100*SECOND+n*50_000_000, .04, True)
      expected = baseline.modelV2.to_dict()
      for field in ('frameId', 'frameIdExtra', 'timestampEof', 'frameDropStats'):
        expected.pop(field)
      assert fields == expected
    assert data.frameDropStats.count == 1 and data.frameDropStats.windowEndMonoTime == 101*SECOND
  # No additional inference, timer or IPC was inserted into the modeld loop.
  source = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/modeld/fill_model_msg.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'fill_model_msg')
  update = [node for node in ast.walk(function) if isinstance(node, ast.Call) and
            isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == 'publish_state.frame_drops.update']
  assert len(update) == 1 and [ast.unparse(arg) for arg in update[0].args] == ['vipc_frame_id', 'timestamp_eof']
