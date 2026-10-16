import importlib.util
from pathlib import Path
import numpy as np
import pytest

spec = importlib.util.spec_from_file_location('probe_device_recurrence', Path(__file__).with_name('probe_device_recurrence.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('corrupt', [None, 'difference', 'nan', 'shape'])
def test_sequential_isolated_probes_and_output_checks(monkeypatch, tmp_path, corrupt):
  modes = []
  def child(args, *, env, check, timeout):
    assert check and timeout == 920
    mode = env['CARROT_TRT_DEVICE_STATE']
    assert mode == str(len(modes))
    modes.append(mode)
    values = np.zeros((32, 2), np.float32)
    if mode == '1':
      if corrupt == 'difference': values[-1, 0] = 1
      if corrupt == 'nan': values[-1, 0] = np.nan
      if corrupt == 'shape': values = values[:31]
    np.save(args[-1], values, allow_pickle=False)
  monkeypatch.setattr(probe.subprocess, 'run', child)
  if corrupt:
    with pytest.raises((ValueError, AssertionError)):
      probe.check(tmp_path, {'sha256': 'test'})
  else:
    probe.check(tmp_path, {'sha256': 'test'})
  assert modes == ['0', '1']
