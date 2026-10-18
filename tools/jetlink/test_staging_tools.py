"""Offline tools must not claim model safety or compete with a live Jetson."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

import bench_input_staging as bench
from simulate_pipeline import simulate


def test_pc_benchmark_does_not_contact_systemd(monkeypatch):
  monkeypatch.setattr(bench, 'is_jetson', lambda: False)
  monkeypatch.setattr(bench.subprocess, 'run', lambda *a, **kw: pytest.fail('PC contacted host service'))
  bench.require_offline_host()


@pytest.mark.parametrize('fields,accepted', [
  ('LoadState=loaded\nActiveState=inactive\n', True),
  ('LoadState=loaded\nActiveState=failed\n', True),
  ('LoadState=loaded\nActiveState=active\n', False),
  ('LoadState=loaded\nActiveState=activating\n', False),
  ('LoadState=not-found\nActiveState=inactive\n', False),
  ('garbled output', False),
])
def test_jetson_benchmark_refuses_live_or_unknown_service(monkeypatch, fields, accepted):
  monkeypatch.setattr(bench, 'is_jetson', lambda: True)
  monkeypatch.setattr(bench.subprocess, 'run', lambda *a, **kw: NS(stdout=fields))
  if accepted:
    bench.require_offline_host()
  else:
    with pytest.raises(RuntimeError, match='inference service'):
      bench.require_offline_host()


def test_pipeline_cannot_create_more_fresh_frames_than_the_camera_produces():
  gpu = np.full(1000, 25.0)
  serial = simulate(12, gpu, 4, pipelined=False)
  pipeline = simulate(12, gpu, 4, pipelined=True)
  assert serial['produced'] == pipeline['produced'] == 1000
  assert serial['mean_hz'] == pipeline['mean_hz'] == 20
  assert serial['age_mean_ms'] == pipeline['age_mean_ms'] == 41


def test_idle_cpu_cannot_make_a_serial_recurrent_gpu_exceed_its_service_rate():
  gpu = np.full(1000, 55.0)
  pipeline = simulate(12, gpu, 4, pipelined=True)
  assert pipeline['produced'] < 1000
  assert pipeline['mean_hz'] == pytest.approx(1000 / 55)
  assert pipeline['dropped_before_output'] > 0


def test_pipeline_only_shows_a_throughput_gain_when_serial_stages_exceed_the_period():
  gpu = np.full(1000, 25.0)
  serial = simulate(40, gpu, 4, pipelined=False)
  pipeline = simulate(40, gpu, 4, pipelined=True)
  assert serial['mean_hz'] < 15
  assert pipeline['mean_hz'] == 20
  assert pipeline['produced'] == 1000
