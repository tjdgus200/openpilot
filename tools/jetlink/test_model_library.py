import json
import sys
from types import SimpleNamespace
import threading

import pytest

from openpilot.common import big_model_catalog as C
from openpilot.selfdrive.modeld import model_library as library
from tools.jetlink import model_library as wire  # noqa: TID251 -- standalone host protocol


@pytest.fixture(autouse=True)
def official_or_pv5_package(monkeypatch, tmp_path):
  # Official two-model behavior has no PV5 USB pin. Paired-package cases below
  # supply one explicitly; all original official selection assertions remain.
  monkeypatch.setattr(C, 'USB_PIN', tmp_path / 'usb-pv5.json')
  monkeypatch.setattr(C, 'USB_DIRECTORY', tmp_path)


def pv5_manifest(key='n16'):
  path = C.ROOT / 'openpilot/selfdrive/modeld/jetlink/usb_host_sha256_release.json'
  value = json.loads(path.read_text())
  spec = C.model_spec(key)
  value['model'].update(sha256=spec['sha256'], size=spec['nbytes'])
  value['profile'] = 'test-' + key
  value['source_commit'] = {'n16': 'a', 'flip-flop': 'b', 'ctv3m': 'c'}[key] * 40
  return value


def test_pv5_usb_package_exposes_only_matching_models(store):
  C.USB_PIN.write_text(json.dumps(pv5_manifest()))
  assert set(C.available()) == {'n16'}
  assert C.freeze_boot() == 'n16'
  with pytest.raises(ValueError, match='not available'):
    C.select('flip-flop')
  assert C.preferences()['selected'] == 'n16'


@pytest.mark.parametrize('value', [{}, {'model': {}}, {'model': {'sha256': 'wrong', 'size': 1}}])
def test_unknown_pv5_package_never_guesses_a_model(value):
  C.USB_PIN.write_text(json.dumps(value))
  with pytest.raises(ValueError, match='USB'):
    C.available()


@pytest.mark.parametrize('model', [None, [], 'n16'])
def test_malformed_pv5_package_model_fails_closed(model):
  value = pv5_manifest()
  value['model'] = model
  C.USB_PIN.write_text(json.dumps(value))
  with pytest.raises(ValueError, match='USB'):
    C.available()


def test_per_model_pv5_packages_preserve_reboot_selection(store, monkeypatch):
  C.USB_PIN.write_text(json.dumps(pv5_manifest()))
  for key in ('n16', 'flip-flop'):
    (C.USB_DIRECTORY / ('pv5-usb-' + key + '.json')).write_text(json.dumps(pv5_manifest(key)))
  assert set(C.available()) == {'n16', 'flip-flop'}
  assert C.freeze_boot() == 'n16'
  C.select('flip-flop')
  assert C.effective_id() == C.freeze_boot() == 'n16'
  assert C.usb_release(C.effective_id())['profile'] == 'test-n16'
  monkeypatch.setattr(C, 'boot_id', lambda: 'boot-two')
  assert C.freeze_boot() == 'flip-flop'
  assert C.usb_release(C.effective_id())['model']['sha256'] == C.model_spec('flip-flop')['sha256']
  C.select('n16')
  assert C.effective_id() == 'flip-flop'


@pytest.mark.parametrize('fault', ['model', 'commit', 'invalid'])
def test_bad_per_model_package_is_never_offered(store, fault):
  C.USB_PIN.write_text(json.dumps(pv5_manifest()))
  value = pv5_manifest('flip-flop')
  if fault == 'model':
    value['model']['sha256'] = 'c' * 64
  elif fault == 'commit':
    value['source_commit'] = pv5_manifest()['source_commit']
  else:
    value['integrity'] = 'none'
  (C.USB_DIRECTORY / 'pv5-usb-flip-flop.json').write_text(json.dumps(value))
  with pytest.raises(ValueError, match='USB|source commits'):
    C.available()


def test_model_choice_does_not_use_retired_or_arbitrary_paths(store):
  C.USB_PIN.write_text(json.dumps(pv5_manifest()))
  for key in ('md-v3', '../n16', 'unknown'):
    with pytest.raises(ValueError, match='unknown or retired'):
      C.usb_release(key)


def test_ctv3m_requires_pv5_package_and_stays_frozen_until_reboot(store, monkeypatch):
  assert 'ctv3m' not in C.available()  # No official signed CTV3M host was published.
  with pytest.raises(ValueError, match='not available'):
    C.select('ctv3m')
  C.USB_PIN.write_text(json.dumps(pv5_manifest()))
  for key in ('n16', 'flip-flop', 'ctv3m'):
    (C.USB_DIRECTORY / ('pv5-usb-' + key + '.json')).write_text(json.dumps(pv5_manifest(key)))
  assert set(C.available()) == {'n16', 'flip-flop', 'ctv3m'}
  assert C.freeze_boot() == 'n16'
  C.select('ctv3m')
  assert C.freeze_boot() == C.effective_id() == 'n16'
  monkeypatch.setattr(C, 'boot_id', lambda: 'ctv3m-boot')
  assert C.freeze_boot() == 'ctv3m'
  assert C.usb_release(C.effective_id())['source_commit'] == 'c' * 40
  assert not C.noo('ctv3m')
  C.select('flip-flop')
  assert C.effective_id() == 'ctv3m'


@pytest.mark.parametrize('fault', ['model', 'source'])
def test_ctv3m_rejects_other_models_package_and_duplicate_runtime_id(store, fault):
  C.USB_PIN.write_text(json.dumps(pv5_manifest()))
  value = pv5_manifest('ctv3m')
  if fault == 'model':
    value['model'] = pv5_manifest('flip-flop')['model']
  else:
    value['source_commit'] = pv5_manifest('n16')['source_commit']
  (C.USB_DIRECTORY / 'pv5-usb-ctv3m.json').write_text(json.dumps(value))
  with pytest.raises(ValueError, match='model|source commits'):
    C.available()


@pytest.fixture
def store(monkeypatch, tmp_path):
  monkeypatch.setenv('CARROT_BIG_MODEL_DIR', str(tmp_path))
  monkeypatch.setattr(C, 'boot_id', lambda: 'boot-one')
  library._SIZES.clear()
  return tmp_path


def test_selection_waits_for_real_reboot_and_recovers_corrupt_settings(store, monkeypatch):
  assert C.freeze_boot() == 'n16'
  C.select('flip-flop')
  assert C.preferences()['selected'] == 'flip-flop'
  assert C.freeze_boot() == C.effective_id() == 'n16'
  monkeypatch.setattr(C, 'boot_id', lambda: 'boot-two')
  assert C.freeze_boot() == C.effective_id() == 'flip-flop'
  (store / 'selection.json').write_text('{invalid')
  assert C.preferences()['selected'] == 'n16'
  assert C.effective_id() == 'flip-flop'


@pytest.mark.parametrize('key', ['../n16', '', 'unknown', 'https://example/model'])
def test_only_reviewed_models_selectable(store, key):
  with pytest.raises(ValueError):
    C.select(key)
  assert not (store / 'selection.json').exists()


def test_official_inputs_and_retired_legacy_alias():
  assert set(C.available()) == {'n16', 'flip-flop'}
  assert C.catalog()['md-v3']['retired']
  assert C.noo('n16')
  assert not C.noo('md-v3') and not C.noo('flip-flop')
  assert C.model_spec('md-v3') == C.model_spec('flip-flop')
  assert C.catalog()['md-v3']['egpu']['sha256'] != C.catalog()['flip-flop']['egpu']['sha256']
  sys.path.insert(0, str(C.ROOT / 'third_party/jetlink'))
  from jetlink.spec import ModelSpec
  from openpilot.selfdrive.modeld.jetlink.contracts import validate_model_spec, model_name

  for key in C.catalog():
    spec = ModelSpec.from_dict(C.model_spec(key))
    assert validate_model_spec(spec) is spec
    label = 'flip-flop' if key == 'md-v3' else key
    assert model_name(spec) == C.catalog()[label]['display_name']
  from openpilot.selfdrive.modeld.generic_model_runtime import model_metadata

  for key, fixture in [('md-v3', 'md_v3_metadata.json'), ('flip-flop', 'flip_flop_metadata.json')]:
    raw = json.loads((C.ROOT / 'openpilot/selfdrive/modeld/tests/fixtures' / fixture).read_text())
    checkpoint, slices, states, count = model_metadata(raw)
    expected = C.model_spec(key)
    assert checkpoint == expected['checkpoint']
    assert {k: [v.start, v.stop] for k, v in slices.items()} == expected['output_slices']
    assert count == expected['output_shapes']['outputs'][1]
    assert 'nav_map' not in raw['input_specs']


def test_delete_protects_boot_pending_and_active_but_removes_unused(store):
  C.freeze_boot()
  C.select('flip-flop')
  for key in ('n16', 'flip-flop'):
    with pytest.raises(ValueError):
      library._remove_cache(key)
  files = library.paths('md-v3')
  for path in files[:2]:
    path.write_bytes(b'model')
  files[2].mkdir(parents=True)
  (files[2] / 'model.pkl').write_bytes(b'copy')
  other = store / 'keep'
  other.write_text('keep')
  library._remove_cache('md-v3')
  assert not any(path.exists() for path in files)
  assert other.read_text() == 'keep'
  assert 'hidden' not in C.preferences()
  assert C.read_json(store / 'jetson-delete.json')['models'] == ['md-v3']
  assert 'md-v3' not in C.available()
  C.atomic_json(store / 'state.json', {'active': C.catalog()['md-v3']['egpu']})
  with pytest.raises(ValueError):
    library._remove_cache('md-v3')


def test_cache_inventory_counts_hardlink_once(store):
  import os

  first = store / 'first'
  first.write_bytes(b'123456')
  try:
    os.link(first, store / 'second')
  except OSError:
    pytest.skip('hardlinks unsupported')
  assert library.unique_bytes([store]) == 6


def test_host_deletes_only_unused_identity_and_rejects_inflight(store):
  sys.path.insert(0, str(C.ROOT / 'third_party/jetlink'))
  from jetlink.server.cache import EngineCache

  cache = EngineCache(store, SimpleNamespace(tag=lambda: 'test', suffix='.plan'))
  n16, md = C.model_spec('n16')['sha256'], C.model_spec('md-v3')['sha256']
  for sha in (n16, md):
    for path in (cache.model_path(sha), cache.entry(sha).path, cache.entry(sha).meta_path):
      path.write_bytes(b'123')
  host = SimpleNamespace(lock=threading.Lock(), cache=cache, job=None, loaded=SimpleNamespace(sha256=n16))
  result = wire.host_request(host, {'delete': [n16, md]}, C.ROOT)
  assert result['deleted'] == [md] and result['models'][n16] == 9 and result['models'][md] == 0
  with pytest.raises(ValueError):
    wire.host_request(host, {'delete': ['../unknown']}, C.ROOT)
  host.job = SimpleNamespace(state='building')
  with pytest.raises(ValueError):
    wire.host_request(host, {'delete': []}, C.ROOT)
  host.job.state = 'ready'
  assert wire.host_request(host, {'delete': []}, C.ROOT)['models'][n16] == 9


def test_client_does_no_io_onroad_or_with_old_peer(store, monkeypatch):
  monkeypatch.setattr(wire.time, 'monotonic', lambda: 100)
  params = SimpleNamespace(get_bool=lambda key: key == 'IsOnroad')
  client = object()  # Any attempted I/O would fail.
  wire.Client({wire.CAPABILITY: True}).idle(client, params)
  wire.Client({}).idle(client, SimpleNamespace(get_bool=lambda key: key == 'IsOffroad'))
  assert not (store / 'jetson-library.json').exists()


def test_jetson_inventory_is_not_presented_as_live_after_reboot(store):
  C.atomic_json(store / 'jetson-library.json', {'boot': 'old', 'updated': 100, 'models': {}})
  result = library.status()
  assert not result['jetson_inventory_fresh']
  assert all(row['jetson_bytes'] is None for row in result['models'])


@pytest.mark.parametrize('value', [{'selected': [], 'hidden': [{}]}, {'selected': None}, {'selected': 'gone'}])
def test_bad_preferences_never_select_unreviewed_model(store, value):
  C.atomic_json(store / 'selection.json', value)
  C.atomic_json(store / 'boot-selection.json', dict(value, boot=C.boot_id()))
  assert C.preferences()['selected'] == 'n16'
  assert C.freeze_boot() == C.effective_id() == 'n16'


def test_alias_delete_keeps_shared_selected_jetson_model(store):
  C.freeze_boot()
  C.select('flip-flop')
  library._remove_cache('md-v3')
  sent = []
  client = SimpleNamespace(
    _next_seq=lambda: 1,
    t=SimpleNamespace(send_json=lambda *args: sent.append(args)),
    _expect=lambda *args: SimpleNamespace(payload=json.dumps({'models': {}, 'deleted': []}).encode()),
  )
  wire.Client({wire.CAPABILITY: True})._exchange(client)
  assert sent[0][2] == {'delete': []}
  assert C.read_json(store / 'jetson-delete.json')['models'] == []


def test_catalog_releases_are_signed_and_match_exact_onnx_contracts():
  from update_host import validate_manifest, verify_signature
  for key, entry in C.catalog().items():
    if entry.get('requires_pv5_usb'):
      assert key == 'ctv3m' and 'jetson_release' not in entry
      continue  # USB owner policy is separate; never fabricate an official signature.
    release = json.loads((C.DIRECTORY / entry['jetson_release']).read_text())
    validate_manifest(release)
    verify_signature(release)
    assert release['model']['sha256'] == C.model_spec(key)['sha256']
    assert release['model']['size'] == C.model_spec(key)['nbytes']


def test_ctv3m_pin_and_existing_queue_contract():
  import numpy as np
  from jetlink.spec import ModelSpec
  from jetlink.queues import StatefulQueues
  from jetlink.output_projection import eligible
  from openpilot.selfdrive.modeld.jetlink.contracts import validate_model_spec, parse_outputs, model_name
  from openpilot.selfdrive.modeld.parse_model_outputs import Parser

  spec = ModelSpec.from_dict(C.model_spec('ctv3m'))
  validate_model_spec(spec)
  assert spec.sha256 == '404a18cfd86d29637d20c697dfde245bb47c666ae016730ab674c65f4d1e1aa4'
  assert spec.nbytes == 766354845 and not eligible(spec)
  assert model_name(spec) == 'CTV3M (bf3e3631b3)'
  assert 'f78ed37d-afad-4dbc-8050-40ea885eedde/12864' in spec.checkpoint
  reference = C.model_spec('flip-flop')
  for key in ('input_shapes', 'output_shapes', 'output_slices', 'frame_skip'):
    assert spec.to_dict()[key] == reference[key]
  assert parse_outputs(Parser(), spec, np.zeros(spec.output_nelem, np.float32))['plan'].shape == (1, 33, 15)
  dest = {key: np.zeros(shape, np.float16 if key in ('new_img', 'state_img_q') else np.float32)
          for key, shape in spec.input_shapes.items()}
  queues = StatefulQueues(spec, dest)
  image = np.arange(np.prod(spec.warped_shape), dtype=np.uint8).reshape(spec.warped_shape)
  queues.step_into(image, np.zeros(spec.packed_nelem, np.float32), dest)
  assert np.array_equal(dest['new_img'], image.astype(np.float16))
  outputs = {key: np.ones(shape, dest[key.removeprefix('next_')].dtype if key.startswith('next_') else np.float32)
             for key, shape in spec.output_shapes.items()}
  queues.feedback(outputs)
  assert all(np.all(state == 1) for state in queues.state.values())
  queues.reset()
  assert all(not np.any(state) for state in queues.state.values())
  outputs['outputs'][0, 0] = np.nan
  with pytest.raises(FloatingPointError):
    queues.feedback(outputs)


def test_ctv3m_precompiled_identity_is_pinned_without_network(monkeypatch):
  from openpilot.selfdrive.modeld import big_model
  entry = C.catalog()['ctv3m']
  monkeypatch.setattr(big_model, 'urlopen', lambda *a, **kw: pytest.fail('catalog pin fetched network'))
  value = big_model.fetch_manifest(entry['manifest_url'])
  assert json.loads((C.DIRECTORY / 'ctv3m-egpu.json').read_text()) == entry['egpu']
  assert value.sha256 == '4937fc6f04367b28328fe3bc0d1463a245725d039ddaab29406cc565fd5c8ed4'
  assert value.size == 773082315
  assert value.url.startswith('https://huggingface.co/buckets/commaai/openpilot-lfs/resolve/sha256/')


def test_ctv3m_missing_pv5_pin_does_not_fall_back_to_another_models_signed_host(store):
  import os
  import subprocess
  code = '''
from pathlib import Path
from openpilot.common import big_model_catalog as C
C.USB_PIN=C.storage()/'absent-usb-pin.json'
C.atomic_json(C.storage()/'boot-selection.json',{'boot':C.boot_id(),'selected':'ctv3m'})
original=Path.is_file
Path.is_file=lambda p:p.as_posix()=='/TICI' or original(p)
from openpilot.common.jetson_model import selected_release
try:
  selected_release(C.storage()/'host_release.json',required=True)
except ValueError as e:
  assert 'matching PV5 USB package' in str(e)
else:
  raise AssertionError('unpublished signed CTV3M host was selected')
'''
  subprocess.run([sys.executable, '-P', '-c', code],
                 env=dict(os.environ, PYTHONPATH=str(C.ROOT)), check=True)


def test_fresh_runtime_import_uses_same_model_for_both_backends(store):
  import os
  import subprocess
  code = '''
from pathlib import Path
from openpilot.common import big_model_catalog as C
# Exercise official multi-model delivery without the PV5-only package pin.
C.USB_PIN = C.storage() / 'absent-usb-pin.json'
C.select('flip-flop'); C.freeze_boot()
original = Path.is_file
Path.is_file = lambda path: path.as_posix() == '/TICI' or original(path)
from openpilot.common.jetson_model import read_spec, selected_release
from openpilot.selfdrive.modeld.big_model import DEFAULT_MANIFEST_URL, fetch_manifest
assert fetch_manifest(DEFAULT_MANIFEST_URL).sha256 == C.catalog()['flip-flop']['egpu']['sha256']
assert read_spec() == C.model_spec('flip-flop')
assert selected_release(C.storage() / 'host_release.json', required=True)['model']['sha256'] == read_spec()['sha256']
C.select('n16')
assert read_spec() == C.model_spec('flip-flop')
assert C.effective_id() == 'flip-flop'
'''
  subprocess.run([sys.executable, '-P', '-c', code], env=dict(os.environ, PYTHONPATH=str(C.ROOT)), check=True)


def test_fresh_pv5_runtime_matches_preserved_usb_package(store):
  import os
  import subprocess
  code = '''
from pathlib import Path
from openpilot.common import big_model_catalog as C
assert 'n16' in C.available()
assert C.freeze_boot() == 'n16'
original = Path.is_file
Path.is_file = lambda path: path.as_posix() == '/TICI' or original(path)
from openpilot.common.jetson_model import read_spec, selected_release
spec = read_spec()
assert spec == C.model_spec('n16')
release = selected_release(C.USB_PIN.with_name('host_release.json'), required=True)
assert release['model']['sha256'] == spec['sha256']
assert release['model']['size'] == spec['nbytes']
assert C.effective_id() == 'n16'
'''
  subprocess.run([sys.executable, '-P', '-c', code], env=dict(os.environ, PYTHONPATH=str(C.ROOT)), check=True)


@pytest.mark.parametrize('key', ['n16', 'flip-flop'])
def test_published_pv5_selection_uses_frozen_models_own_usb_release(store, key):
  import os
  import subprocess
  code = '''
import sys
from pathlib import Path
from openpilot.common import big_model_catalog as C
key=sys.argv[1]
assert set(C.available()) == {'n16','flip-flop'}
C.select(key)
assert C.freeze_boot() == key
original=Path.is_file
Path.is_file=lambda path: path.as_posix() == '/TICI' or original(path)
from openpilot.common.jetson_model import read_spec,selected_release
release=selected_release(C.USB_PIN.with_name('host_release.json'),required=True)
assert release == C.usb_release(key)
assert release['model']['sha256'] == read_spec()['sha256'] == C.model_spec(key)['sha256']
assert release['model']['size'] == read_spec()['nbytes']
C.select('flip-flop' if key=='n16' else 'n16')
assert C.effective_id() == key
assert selected_release(C.USB_PIN.with_name('host_release.json'),required=True)==release
'''
  subprocess.run([sys.executable, '-P', '-c', code, key],
                 env=dict(os.environ, PYTHONPATH=str(C.ROOT)), check=True)


def retire(monkeypatch, *keys):
  models = C.catalog()
  for key in keys:
    models[key]['retired'] = True
  monkeypatch.setattr(C, 'catalog', lambda: models)


def test_retirement_preserves_running_boot_and_falls_back_next_boot(store, monkeypatch):
  C.atomic_json(store / 'selection.json', {'selected': 'md-v3'})
  C.atomic_json(store / 'boot-selection.json', {'selected': 'md-v3', 'boot': C.boot_id()})
  retire(monkeypatch, 'md-v3')
  assert C.effective_id() == C.freeze_boot() == 'md-v3'
  assert C.preferences()['selected'] == 'n16'
  assert 'md-v3' not in C.available()
  with pytest.raises(ValueError):
    C.select('md-v3')
  offroad = SimpleNamespace(get_bool=lambda key: key == 'IsOffroad')
  assert library.reconcile_retired(offroad) == []
  monkeypatch.setattr(C, 'boot_id', lambda: 'next-boot')
  assert C.freeze_boot() == 'n16'
  assert library.reconcile_retired(offroad) == ['md-v3']


def test_curated_retirement_only_deletes_offroad_after_active_transition(store, monkeypatch):
  C.freeze_boot()
  retire(monkeypatch, 'md-v3')
  target = library.paths('md-v3')[0]
  target.write_bytes(b'old')
  keep = store / 'keep.txt'
  keep.write_text('keep')
  onroad = SimpleNamespace(get_bool=lambda key: key == 'IsOnroad')
  offroad = SimpleNamespace(get_bool=lambda key: key == 'IsOffroad')
  assert library.reconcile_retired(onroad) == [] and target.exists()
  C.atomic_json(store / 'state.json', {'active': C.catalog()['md-v3']['egpu']})
  assert library.reconcile_retired(offroad) == [] and target.exists()
  C.atomic_json(store / 'state.json', {'active': C.catalog()['n16']['egpu']})
  assert library.reconcile_retired(offroad) == ['md-v3']
  assert not target.exists() and keep.read_text() == 'keep'
  assert C.read_json(store / 'jetson-delete.json')['models'] == ['md-v3']
  assert 'md-v3' not in {m['id'] for m in library.status()['models']}


def test_hidden_preferences_migrate_to_full_curated_list(store):
  C.atomic_json(store / 'selection.json', {'selected': 'n16', 'hidden': ['md-v3', 'flip-flop']})
  assert {m['id'] for m in library.status()['models']} == set(C.available())


def test_retired_alias_never_deletes_available_flip_onnx(store, monkeypatch):
  C.freeze_boot()
  C.select('flip-flop')
  retire(monkeypatch, 'md-v3')
  library._remove_cache('md-v3')
  sent = []
  reply = {'models': {}, 'deleted': []}
  client = SimpleNamespace(_next_seq=lambda: 1, t=SimpleNamespace(send_json=lambda *args: sent.append(args)),
                           _expect=lambda *args: SimpleNamespace(payload=json.dumps(reply).encode()))
  wire.Client({wire.CAPABILITY: True})._exchange(client)
  assert sent[-1][2] == {'delete': []}
  assert C.read_json(store / 'jetson-delete.json')['models'] == []
  C.select('n16')
  reply['deleted'] = []
  wire.Client({wire.CAPABILITY: True})._exchange(client)
  assert sent[-1][2] == {'delete': []}
  assert C.read_json(store / 'jetson-delete.json')['models'] == []
