import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

import runtime_port_options as options


@pytest.mark.parametrize('supported,expected', [(True, '1'), (False, '0')])
def test_bundled_aio_default_is_limited_to_known_comma_devices(monkeypatch, supported, expected):
  monkeypatch.setattr(options, 'comma_aio_supported', lambda: supported)
  env = {}
  options.apply(environ=env)
  assert env == {'CARROT_TRT_DEVICE_STATE': '1', 'CARROT_JETLINK_AIO_WRITE': expected}


def test_process_can_explicitly_disable_ported_optimizations(monkeypatch):
  monkeypatch.setattr(options, 'comma_aio_supported', lambda: True)
  env = dict.fromkeys(options.KEYS, '0')
  assert options.apply(environ=env) == env == dict.fromkeys(options.KEYS, '0')


@pytest.mark.parametrize('model', [b'comma tici', b'comma tizi', b'comma mici'])
def test_c3_c3x_and_c4_enable_aio_with_the_matching_kernel(monkeypatch, model):
  monkeypatch.setattr(options.platform, 'system', lambda: 'Linux')
  monkeypatch.setattr(options.platform, 'machine', lambda: 'aarch64')
  monkeypatch.setattr(options.platform, 'release', lambda: '4.9.103')
  monkeypatch.setattr(options, 'Path', lambda path: NS(read_bytes=lambda: model + b'\0\n'))
  assert options.comma_aio_supported()
  env = {}
  assert options.apply(environ=env)['CARROT_JETLINK_AIO_WRITE'] == '1'
  assert env['CARROT_TRT_DEVICE_STATE'] == '1'


@pytest.mark.parametrize('system,machine,kernel,model', [
  ('Linux', 'aarch64', '4.9.103', b'unknown'),
  ('Linux', 'aarch64', '4.9.103', b'comma mici future'),
  ('Linux', 'aarch64', '5.15.0', b'comma mici'),
  ('Linux', 'x86_64', '4.9.103', b'comma mici'),
  ('Windows', 'aarch64', '4.9.103', b'comma mici'),
])
def test_other_platforms_do_not_gain_an_aio_default(monkeypatch, system, machine, kernel, model):
  monkeypatch.setattr(options.platform, 'system', lambda: system)
  monkeypatch.setattr(options.platform, 'machine', lambda: machine)
  monkeypatch.setattr(options.platform, 'release', lambda: kernel)
  monkeypatch.setattr(options, 'Path', lambda path: NS(read_bytes=lambda: model))
  env = {}
  assert not options.comma_aio_supported()
  assert options.apply(environ=env)['CARROT_JETLINK_AIO_WRITE'] == '0'


def test_unreadable_device_identity_does_not_enable_aio(monkeypatch):
  monkeypatch.setattr(options.platform, 'system', lambda: 'Linux')
  monkeypatch.setattr(options.platform, 'machine', lambda: 'aarch64')
  monkeypatch.setattr(options.platform, 'release', lambda: '4.9.103')
  def denied():
    raise PermissionError('device identity unavailable')
  monkeypatch.setattr(options, 'Path', lambda path: NS(read_bytes=denied))
  assert not options.comma_aio_supported()


def test_missing_or_malformed_release_choices_never_silently_enable(tmp_path):
  path = tmp_path / 'options.json'
  assert options.apply(path, {}) == {}
  path.write_text(json.dumps({'CARROT_TRT_DEVICE_STATE': True}))
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
