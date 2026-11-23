import hashlib
from types import SimpleNamespace

import pytest

from openpilot.common import big_model_catalog
from openpilot.selfdrive.modeld import internal_model as native


@pytest.fixture
def boot(tmp_path, monkeypatch):
  identity = ['first-boot']
  monkeypatch.setattr(native, 'ROOT', tmp_path / 'cache')
  monkeypatch.setattr(big_model_catalog, 'storage', lambda: tmp_path / 'model-library-lock')
  monkeypatch.setattr(native, 'BOOT_PATH', tmp_path / 'boot.json')
  monkeypatch.setattr(native, 'available', lambda: True)
  monkeypatch.setattr(native, 'boot_id', lambda: identity[0])
  monkeypatch.setattr(native, '_build_identity', lambda: {'source': native.ONNX_SHA256})
  return identity


def mock_prepare(monkeypatch):
  events = []

  def download(path):
    events.append('download')
    path.write_bytes(b'onnx')

  def compile_model(onnx, target):
    events.append('compile')
    assert onnx.read_bytes() == b'onnx'
    artifact = b'built-locally'
    target.write_bytes(artifact)
    return {'size': len(artifact), 'sha256': hashlib.sha256(artifact).hexdigest()}

  monkeypatch.setattr(native, '_download', download)
  monkeypatch.setattr(native, '_compile', compile_model)
  return events


def test_selection_latches_on_real_reboot_and_reuses_verified_artifact(boot, monkeypatch):
  events = mock_prepare(monkeypatch)
  native.prepare_boot()
  assert native.status()['effective'] == native.CD210
  native.select(native.VEGA)
  native.prepare_boot()
  assert native.status()['reboot_required']
  assert native.prepared_path() is None and not events
  boot[0] = 'second-boot'
  native.prepare_boot()
  assert native.prepared_path().name == 'vega2-qcom.pkl'
  assert native.status()['effective'] == native.CD210
  assert events == ['download', 'compile']
  native.starting()
  assert native.status()['state'] == 'preparing'
  native.record_loaded(True)
  assert native.status()['effective'] == native.VEGA
  assert not native.status()['reboot_required']
  boot[0] = 'third-boot'
  native.prepare_boot()
  assert events == ['download', 'compile']
  assert native.prepared_path().is_file()


def test_preparation_error_and_incomplete_model_start_fall_back(boot, monkeypatch):
  def unavailable(_):
    raise TimeoutError
  monkeypatch.setattr(native, '_download', unavailable)
  native.select(native.VEGA)
  native.prepare_boot()
  assert native.prepared_path() is None and native.status()['reboot_required']
  boot[0] = 'failure-boot'
  native.prepare_boot()
  assert native.status()['state'] == 'fallback'
  assert native.status()['effective'] == native.CD210
  assert native.status()['selected'] == native.VEGA
  assert native.prepared_path() is None

  mock_prepare(monkeypatch)
  boot[0] = 'next-boot'
  native.prepare_boot()
  assert native.prepared_path() is not None
  native.starting()
  assert native.prepared_path() is None  # prior modeld never produced valid output
  assert native.status()['state'] == 'fallback'


def test_other_car_cannot_select_alternate(boot, monkeypatch):
  cp = SimpleNamespace(brand='hyundai', carFingerprint='KIA_PV5', notCar=False)
  assert native.supported_car(cp)
  cp.notCar = True
  assert not native.supported_car(cp)
  cp.notCar = False
  cp.carFingerprint = 'HYUNDAI_IONIQ_5_PE'
  assert not native.supported_car(cp)
  monkeypatch.setattr(native, 'available', lambda: False)
  native.prepare_boot()
  assert native.status() is None and not native.BOOT_PATH.exists()
  with pytest.raises(ValueError):
    native.select(native.VEGA)


def test_invalid_model_settings_default_to_cd210(boot, monkeypatch):
  events = mock_prepare(monkeypatch)
  native.atomic_json(native.ROOT / 'selection.json', {'selected': ['invalid']})
  native.prepare_boot()
  assert native.status()['selected'] == native.CD210
  assert native.prepared_path() is None and not events


def test_compiled_file_with_same_size_but_wrong_hash_is_not_loaded(boot, monkeypatch):
  mock_prepare(monkeypatch)
  native.select(native.VEGA)
  boot[0] = 'next-boot'
  native.prepare_boot()
  pkl = native.prepared_path()
  pkl.write_bytes(b'?' * pkl.stat().st_size)
  assert native.prepared_path() is None
  assert native.status()['state'] == 'fallback'
