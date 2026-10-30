"""No devices: real joining lifecycle and parse/fallback boundary under faults."""
from concurrent.futures import Future
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.selfdrive.modeld.jetlink import contracts, model
from openpilot.selfdrive.modeld.parse_model_outputs import Parser
from test_hotplug_resources import joining_model
from test_nav_rl import SPEC, buffers, frame_inputs


@pytest.mark.parametrize('failure', ['parsed_nan', 'parsed_inf', 'timeout', 'wrong_spec'])
def test_invalid_external_result_closes_resets_and_rejoins_fresh(monkeypatch, tmp_path, failure):
  m = joining_model(monkeypatch)
  monkeypatch.setattr(model, 'parse_outputs', contracts.parse_outputs)
  m.spec = SPEC
  m.packed, m.views = buffers()
  m.active = failure != 'wrong_spec'
  events = []
  class Corrupt(Parser):
    def parse_outputs(self, outputs):
      parsed = super().parse_outputs(outputs)
      if failure in ('parsed_nan', 'parsed_inf'):
        parsed['plan'][0, 0, 0] = np.nan if failure == 'parsed_nan' else np.inf
      return parsed
  m.parser = Corrupt()
  def infer(*a, **kw):
    events.append('infer')
    if failure in ('timeout', 'wrong_spec'):
      raise TimeoutError('mock deadline') if failure == 'timeout' else ValueError('model identity mismatch')
    return np.zeros(SPEC.output_nelem, np.float32)
  m.client = NS(image_buffer=lambda: None, infer=infer, timings=(0, 0, 0), close=lambda: events.append('close'))
  m.small.reset = lambda: events.append('reset')
  m.small.run = lambda *a: events.append('local') or 'local'
  m.warp = lambda *a, prepare=None, output_buffer=None: prepare()
  fault = tmp_path / 'fault'
  monkeypatch.setattr(model, 'FAULT', fault)
  assert m.run({}, {}, frame_inputs(), False) == 'local'
  assert events == ['infer', 'close', *([] if failure == 'wrong_spec' else ['reset']), 'local']
  assert fault.exists() == (failure != 'wrong_spec')
  assert m.client is None and not m.active and m.next_join > 0

  # Reconnect is a new client with cleared context and an explicit first reset.
  m.parser, m.next_join = Parser(), 0
  resets = []
  def fresh_infer(image, packed, frame, reset, source_sof):
    resets.append(reset)
    return np.zeros(SPEC.output_nelem, np.float32)
  ready = Future()
  ready.set_result(NS(spec=SPEC, image_buffer=lambda: None, infer=fresh_infer, timings=(0, 0, 0), close=lambda: None))
  m.connection = NS(future=ready)
  assert isinstance(m.run({}, {}, frame_inputs(), False), dict)
  assert resets == [True] and m.active


def test_disabled_link_never_opens_or_prepares_and_keeps_native_model(monkeypatch):
  m = joining_model(monkeypatch)
  m.warp, m.ready = None, False
  monkeypatch.setattr(model, 'ClientConnection', lambda: pytest.fail('disabled external connection'))
  monkeypatch.setattr(model, 'WarpPreparation', lambda *a: pytest.fail('disabled GPU worker'))
  for _ in range(32):
    assert m.run({}, {}, {}, False) == 'local'
  assert not m.active and m.client is m.connection is m.preparation is None
