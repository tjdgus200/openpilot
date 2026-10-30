"""Post-parser faults must not reach a publisher, even with finite raw outputs."""
from dataclasses import replace

import numpy as np
import pytest

from jetlink import output_projection as projection
from jetlink.spec import ModelSpec
from openpilot.common.big_model_catalog import model_spec
from openpilot.selfdrive.modeld.jetlink.contracts import DEFAULT_SPEC, JETSON_SPEC, parse_outputs
from openpilot.selfdrive.modeld.output_validation import NonFiniteModelOutput, validate_parsed_outputs
from openpilot.selfdrive.modeld.parse_model_outputs import Parser


def test_guard_does_not_overflow_reductions_or_copy_valid_values():
  data = np.full(100, np.finfo(np.float32).max, np.float32)
  outputs = {'plan': data}
  original = data.tobytes()
  assert validate_parsed_outputs(outputs) is outputs
  assert outputs['plan'] is data and data.tobytes() == original


@pytest.mark.parametrize('bad', [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize('field', ['plan', 'plan_stds', 'hidden_state', 'action'])
def test_every_parsed_array_is_checked_and_never_repaired(bad, field):
  outputs = {'plan': np.zeros((1, 33, 15), np.float32), field: np.array([bad], np.float32)}
  before = {key: value.tobytes() for key, value in outputs.items()}
  with pytest.raises(NonFiniteModelOutput, match='invalid parsed model output'):
    validate_parsed_outputs(outputs)
  assert before == {key: value.tobytes() for key, value in outputs.items()}


@pytest.mark.parametrize('bad', [None, 'finite', np.array(['1']), np.array([object()])])
def test_non_numeric_output_is_rejected(bad):
  with pytest.raises(ValueError, match='type'):
    validate_parsed_outputs({'plan': bad})


@pytest.mark.parametrize('path', ['direct', 'compact', 'legacy'])
@pytest.mark.parametrize('bad', [np.nan, np.inf, -np.inf])
def test_all_contract_parser_branches_validate_after_parsing(path, bad):
  spec = JETSON_SPEC
  if path == 'legacy':
    slices, offset = {}, 0
    for name, at in DEFAULT_SPEC.output_slices.items():
      width = {'plan': 4955, 'lead': 102}.get(name, at.stop - at.start)
      slices[name] = slice(offset, offset + width)
      offset += width
    spec = replace(DEFAULT_SPEC, output_shapes={'outputs': (1, offset)}, output_slices=slices)
  result = np.zeros(projection.COUNT if path == 'compact' else spec.output_nelem, np.float32)
  class Corrupt(Parser):
    def parse_vision_outputs(self, outputs):
      outputs = super().parse_vision_outputs(outputs)
      outputs['pose'][0, 0] = bad  # Finite raw output, corrupted parsed value.
      return outputs
  with pytest.raises(NonFiniteModelOutput):
    parse_outputs(Corrupt(), spec, result)


@pytest.mark.parametrize('spec', [DEFAULT_SPEC, JETSON_SPEC, ModelSpec.from_dict(model_spec('ctv3m')),
                                ModelSpec.from_dict(model_spec('md-v3'))])
def test_valid_parser_result_is_unchanged_for_every_selected_model(spec):
  raw = np.linspace(-1, 1, spec.output_nelem, dtype=np.float32)
  # Independent existing parser path, before the added final guard.
  sliced = {name: raw[None, at].copy() for name, at in spec.output_slices.items()}
  expected = Parser().parse_outputs(sliced)
  actual = parse_outputs(Parser(), spec, raw.copy())
  assert actual.keys() == expected.keys()
  for name in actual:
    np.testing.assert_array_equal(actual[name], expected[name])
