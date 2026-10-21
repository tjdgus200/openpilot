import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import runtime_port_options as options


def test_release_only_enables_gpu_state_and_does_not_publish_aio_choice():
  env = {}
  assert options.apply(environ=env) == {'CARROT_TRT_DEVICE_STATE': '1'}
  assert env == {'CARROT_TRT_DEVICE_STATE': '1'}


def test_process_can_explicitly_disable_gpu_state():
  env = {'CARROT_TRT_DEVICE_STATE': '0'}
  assert options.apply(environ=env) == env == {'CARROT_TRT_DEVICE_STATE': '0'}


@pytest.mark.parametrize('version', [2, 3])
def test_daemon_uses_official_serial_writer_even_with_stale_aio_environment(tmp_path, monkeypatch, version):
  from openpilot.selfdrive.modeld.jetlink import daemon
  from jetlink import protocol as p
  from jetlink.transport.ffs import FfsTransport
  monkeypatch.setenv('CARROT_JETLINK_AIO_WRITE', '1')
  monkeypatch.setattr('jetlink.transport.watchdog.background_thread', lambda: None)
  assert daemon.FfsTransport is FfsTransport
  assert daemon.CarrotTransport._write is FfsTransport._write
  owner = daemon.CarrotTransport.__new__(daemon.CarrotTransport)
  owner._prepare('/unused', None)
  owner.protocol_version = version
  owner._ensure_epfiles = lambda: None
  path = tmp_path / 'serial-endpoint'
  owner.ep_in = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
  original = os.writev
  writes = []
  def writev(fd, buffers):
    writes.append(sum(memoryview(b).nbytes for b in buffers))
    return original(fd, buffers)
  monkeypatch.setattr(os, 'writev', writev)
  payload = b'frame' * 95000
  try:
    owner.send(p.Msg.INFER_REQ, 7, [payload], timeout=2)
    wire = path.read_bytes()
    assert wire[p.HEADER_SIZE:p.HEADER_SIZE + len(payload)] == payload
    assert len(writes) == 1 and writes[0] == len(wire) > 32768
    assert len(wire) % p.GADGET_TX_ALIGN == 0
  finally:
    owner.close()


@pytest.mark.parametrize('value', [{}, {'CARROT_TRT_DEVICE_STATE': 1},
                                  {'CARROT_TRT_DEVICE_STATE': True, 'CARROT_JETLINK_AIO_WRITE': True}])
def test_missing_or_malformed_release_choices_never_silently_enable(tmp_path, value):
  path = tmp_path / 'options.json'
  assert options.apply(path, {}) == {}
  path.write_text(json.dumps(value))
  with pytest.raises(ValueError):
    options.apply(path, {})


def test_hud_import_keeps_top_level_usb_updater_visible():
  root = Path(__file__).resolve().parents[2]
  script = '''
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / "openpilot/selfdrive/carrot/cluster")]
import cluster_live
assert str(root / "openpilot") not in sys.path
import tools.jetlink.usb_release as updater
assert Path(updater.__file__).resolve() == root / "tools/jetlink/usb_release.py"
'''
  result = subprocess.run([sys.executable, '-c', script, str(root)], cwd=root,
                          env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True, text=True, timeout=20)
  assert result.returncode == 0, result.stderr
