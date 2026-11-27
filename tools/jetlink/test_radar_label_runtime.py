import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import radar_label_runtime as runtime


@pytest.fixture
def package(monkeypatch, tmp_path):
  root = tmp_path / 'release'
  assets = root / 'tools/pv5_radar_labels/runtime'
  assets.mkdir(parents=True)
  (root / 'SOURCE_COMMIT').write_text('a' * 40)
  (assets / runtime.LIB).touch()
  (assets / runtime.WORKER).touch(mode=0o700)
  cache = tmp_path / 'cache'
  cache.mkdir()
  for key in ('PV5_RADAR_LABELS', 'PV5_RADAR_LABEL_WORKER', 'PV5_RADAR_LABEL_LIB', 'PV5_RADAR_LABEL_PLAN',
              'PV5_RADAR_LABEL_DIR'):
    monkeypatch.delenv(key, raising=False)
  monkeypatch.setattr(runtime, 'ROOT', root)
  monkeypatch.setattr(runtime, 'RUNTIME', assets)
  monkeypatch.setattr(runtime, 'CACHE', cache / 'pv5-radar-labels')
  return cache


def test_candidate_builds_once_and_only_enables_the_validated_release(package, monkeypatch):
  actions = []
  def run(cmd, check, timeout):
    assert check and timeout > 0
    action, path = cmd[-2], Path(cmd[-1])
    actions.append(action)
    if action == 'build':
      path.write_bytes(b'valid test plan')
    else:
      assert path.read_bytes() == b'valid test plan'
  monkeypatch.setattr(subprocess, 'run', run)
  deadline = time.monotonic() + 5
  assert runtime.prepare_optional(package, deadline) and runtime.prepare_optional(package, deadline)
  assert actions == ['build', 'check', 'check']
  (runtime.ROOT / 'SOURCE_COMMIT').write_text('b' * 40)
  runtime.configure_installed()
  assert 'PV5_RADAR_LABELS' not in os.environ
  (runtime.ROOT / 'SOURCE_COMMIT').write_text('a' * 40)
  runtime.configure_installed()
  assert os.environ['PV5_RADAR_LABELS'] == '1'
  assert Path(os.environ['PV5_RADAR_LABEL_PLAN']).read_bytes() == b'valid test plan'
  assert Path(os.environ['PV5_RADAR_LABEL_WORKER']) == runtime.RUNTIME / runtime.WORKER
  monkeypatch.setenv('PV5_RADAR_LABELS', '0')
  runtime.configure_installed()
  assert os.environ['PV5_RADAR_LABELS'] == '0'


def test_failed_candidate_preserves_plan_and_clears_permission(package, monkeypatch):
  runtime.CACHE.mkdir()
  plan = runtime.CACHE / runtime.PLAN_NAME
  plan.write_bytes(b'previous')
  marker = runtime.ready_file(runtime.CACHE)
  marker.touch()
  actions = []
  def fail(cmd, **kwargs):
    actions.append(cmd[-2])
    raise subprocess.CalledProcessError(1, cmd)
  monkeypatch.setattr(subprocess, 'run', fail)
  assert not runtime.prepare_optional(package, time.monotonic() + 5)
  assert actions == ['check', 'build'] and not marker.exists() and plan.read_bytes() == b'previous'
  runtime.configure_installed()
  assert 'PV5_RADAR_LABELS' not in os.environ


def test_missing_bin_and_expired_probe_budget_fail_soft(package, monkeypatch):
  def no_child(*args, **kwargs):
    pytest.fail('A missing prerequisite or expired validation cannot launch a GPU build')
  monkeypatch.setattr(subprocess, 'run', no_child)
  assert not runtime.prepare_optional(package, time.monotonic() - 1)
  (runtime.RUNTIME / runtime.WORKER).unlink()
  assert not runtime.prepare_optional(package, time.monotonic() + 5)
  assert not runtime.ready_file(runtime.CACHE).exists()


def test_comma_publisher_reuses_one_radar_reader_while_rebinding_pv5_markers(monkeypatch):
  from hud_protocol import SnapshotBuilder
  from openpilot.common import pv5_radar_label_context as labels
  monkeypatch.delenv('PV5_RADAR_LABELS', raising=False)
  reader = MagicMock()
  subscribe = MagicMock(return_value=reader)
  monkeypatch.setattr(labels.RadarContext, '_radar_reader', staticmethod(subscribe))
  marker_sock = MagicMock()
  socket_factory = MagicMock(return_value=marker_sock)
  monkeypatch.setattr(labels.socket, 'AF_UNIX', 1, raising=False)
  monkeypatch.setattr(labels.socket, 'socket', socket_factory)
  builder = SnapshotBuilder.__new__(SnapshotBuilder)
  builder.radar_context = builder.receive_radar = None
  for vehicle in (None, SimpleNamespace(notCar=True, carFingerprint='KIA_PV5'),
                  SimpleNamespace(notCar=False, carFingerprint='OTHER')):
    builder.select_radar_context(vehicle)
    assert builder.radar_context is None
  socket_factory.assert_not_called()
  pv5 = SimpleNamespace(notCar=False, carFingerprint='KIA_PV5')
  marker_sock.bind.side_effect = OSError('address already in use')
  builder.select_radar_context(pv5)
  assert builder.radar_context is builder.receive_radar is None
  marker_sock.close.assert_called_once()
  subscribe.assert_not_called()
  marker_sock.bind.side_effect = None

  for _ in range(45):
    builder.select_radar_context(pv5)
    assert builder.radar_context.receive_radar is reader
    builder.select_radar_context(pv5, enabled=False)
    assert builder.radar_context is None and builder.receive_radar is reader
  subscribe.assert_called_once_with()
  assert marker_sock.close.call_count == 46
  reader.assert_not_called()

  marker_sock.bind.side_effect = OSError('address already in use')
  builder.select_radar_context(pv5)
  assert builder.radar_context is None and builder.receive_radar is reader
  subscribe.assert_called_once_with()


def test_build_accepts_tensorrt_hostmemory_without_a_length(tmp_path, monkeypatch):
  from tools.pv5_radar_labels.native import build_engine

  class HostMemory:
    def __bytes__(self):
      return b'serialized engine'

  trt = MagicMock()
  builder = trt.Builder.return_value
  builder.create_network.return_value.num_inputs = builder.create_network.return_value.num_outputs = 1
  builder.build_serialized_network.return_value = HostMemory()
  monkeypatch.setitem(sys.modules, 'tensorrt', trt)
  monkeypatch.setattr(build_engine, 'verified_model', lambda path: b'ONNX')
  path = tmp_path / 'candidate.plan'
  runtime.build_plan(path)
  assert path.read_bytes() == b'serialized engine'


def test_earlier_worker_cli_is_rejected_before_loading_detector(tmp_path, monkeypatch):
  legacy = b'Error: "usage: radar-label-worker ENGINE.plan PRIVATE_RUN_DIRECTORY [HUD_PACKET]"'
  monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stderr=legacy))
  with pytest.raises(RuntimeError, match='worker failed'):
    runtime.check_plan(tmp_path/'unused.plan')
