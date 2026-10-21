"""Exercise the actual activation probe without CUDA or an auxiliary process."""
import sys
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest

import probe_release as probe


@pytest.mark.parametrize(('fault', 'expected_error'), [
  ('none', None), ('contract', ValueError), ('nonfinite', ValueError),
  ('empty', ValueError), ('load', RuntimeError), ('hud', RuntimeError),
])
def test_activation_keeps_hud_gpu_contract_and_finite_checks_without_cpu_probe(monkeypatch, tmp_path, fault, expected_error):
  calls = []
  spec = {'sha256': 'a' * 64, 'nbytes': 1, 'frame_skip': 1,
          'warped_shape': (1,), 'packed_nelem': 1}
  loaded = NS(spec=NS(**spec, to_dict=lambda: {} if fault == 'contract' else spec),
              host_inputs={}, queues=NS(reset=lambda: calls.append('reset'),
                                       step_into=lambda *args: calls.append('inputs'),
                                       feedback=lambda values: calls.append('feedback')))
  def infer():
    calls.append('infer')
    return {} if fault == 'empty' else {'outputs': np.array([np.nan if fault == 'nonfinite' else 0.])}
  loaded.engine = NS(run=infer)
  host = NS(loaded=loaded, request=lambda *args: calls.append('request'),
            status=lambda *args: {'state': 'failed' if fault == 'load' else 'ready'},
            close=lambda: calls.append('closed'))
  backend = ModuleType('jetlink.server.backends.trt')
  backend.TrtBackend = lambda: object()
  cache = ModuleType('jetlink.server.cache')
  cache.EngineCache = lambda *args: NS()
  session = ModuleType('jetlink.server.session')
  session.EngineHost = lambda *args: host
  session.Request = lambda *args: args
  for name, module in [('server', ModuleType('server')), ('jetlink.server.backends.trt', backend),
                       ('jetlink.server.cache', cache), ('jetlink.server.session', session)]:
    monkeypatch.setitem(sys.modules, name, module)
  monkeypatch.setattr(probe, 'read_spec', lambda root: spec)
  monkeypatch.setattr(sys, 'argv', ['probe_release.py', str(tmp_path)])
  def run(command, **kwargs):
    # Auxiliary CPU models are not an activation prerequisite. A missing or
    # unusable auxiliary process must not be launched by this entry point.
    assert command == [sys.executable, str(probe.ROOT / 'tools/jetlink/hud.py'), '--help']
    assert kwargs['check'] is True and kwargs['timeout'] == 30
    calls.append('hud')
    if fault == 'hud':
      raise RuntimeError('HUD entrypoint failed')
  monkeypatch.setattr(probe, 'subprocess', NS(run=run, DEVNULL=-3))
  if expected_error is None:
    probe.main()
    assert calls == ['hud', 'request', 'reset', 'inputs', 'infer', 'feedback',
                     'inputs', 'infer', 'feedback', 'inputs', 'infer', 'feedback', 'closed']
  else:
    with pytest.raises(expected_error):
      probe.main()
    if fault == 'hud':
      assert calls == ['hud']
    else:
      assert calls[-1] == 'closed'
      assert calls.count('infer') == (1 if fault in ('nonfinite', 'empty') else 0)
