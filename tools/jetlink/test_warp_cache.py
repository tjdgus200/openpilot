"""Local storage and recovery of the optional warp executable (no QCOM needed)."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.modeld.jetlink import warp_cache as cache


@pytest.mark.skipif(os.name != 'posix', reason='Linux compiler mapping')
def test_identity_tracks_actual_layout_runtime_driver_and_sources(tmp_path, monkeypatch):
  compiler = tmp_path / 'libllvm-qcom.so'
  compiler.write_bytes(b'compiler')
  runtime = tmp_path / 'tinygrad'
  runtime.mkdir()
  tiny = runtime / '__init__.py'
  tiny.write_text('')
  # A runtime source can have the same basename as a modeld source.
  (runtime / 'helpers.py').write_text('runtime')
  model = tmp_path / 'modeld'
  jetlink = model / 'jetlink'
  jetlink.mkdir(parents=True)
  for directory, names in ((jetlink, ('warp_cache', 'prepare', 'model', 'warp', 'coherent', 'contracts')),
                           (model, ('compile_modeld', 'helpers', 'precompiled_validation'))):
    for name in names:
      (directory / (name + '.py')).write_text(name)
  monkeypatch.setattr(cache, '__file__', str(jetlink / 'warp_cache.py'))
  read_text = Path.read_text
  def read(path, *args, **kwargs):
    if str(path) == '/proc/self/maps':
      return f'0-1 r-xp 0 0:0 1 {compiler}'
    return read_text(path, *args, **kwargs)
  monkeypatch.setattr(Path, 'read_text', read)
  context = {'device': 'tizi', 'os': 'A', 'machine_id': 'local-unit'}
  gpu = NS(gpu_id=(6, 3, 0), renderer=object())
  flags = {'IMAGE': NS(value=1)}
  spec = NS(sha256='A', warped_shape=(2, 6, 128, 256), frame_skip=4)
  layout = [2048, 1216, 608, 4816896]
  modules = {'tinygrad': NS(Device={'QCOM': gpu}, __file__=str(tiny)), 'tinygrad.helpers': NS(ContextVar=NS(_cache=flags)),
             'openpilot.selfdrive.modeld.jetlink.link': NS(SPEC=spec),
             'openpilot.selfdrive.modeld.precompiled_validation': NS(device_context=lambda: context, camera_sizes=lambda _: [(1928, 1208)]),
             'openpilot.system.camerad.cameras.nv12_info': NS(get_nv12_info=lambda *_: layout)}
  for name, module in modules.items():
    monkeypatch.setitem(sys.modules, name, module)
  def key():
    return cache.identity(1928, 1208)
  keys = {key()}
  assert key() in keys
  for change in (
      lambda: context.update(os='B'), lambda: context.update(machine_id='other'), lambda: setattr(gpu, 'gpu_id', (6, 4, 0)),
      lambda: compiler.write_bytes(b'new driver'), lambda: setattr(spec, 'sha256', 'B'), lambda: setattr(spec, 'frame_skip', 2),
      lambda: layout.__setitem__(0, 2176), lambda: (runtime / 'helpers.py').write_text('edited runtime'),
      lambda: (model / 'helpers.py').write_text('edited helper'), lambda: (jetlink / 'warp.py').write_text('edited fused'),
      lambda: (jetlink / 'coherent.py').write_text('edited allocation'),
      lambda: setattr(flags['IMAGE'], 'value', 0), lambda: monkeypatch.setenv('JETLINK_COHERENT_WARP', 'disabled-test')):
    change()
    updated = key()
    assert updated not in keys
    keys.add(updated)
  with pytest.raises(ValueError):
    cache.identity(1344, 760)
  context['machine_id'] = ''
  with pytest.raises(ValueError):
    key()


@pytest.fixture
def slot(monkeypatch, tmp_path):
  if not hasattr(os, 'O_NOFOLLOW') or os.name != 'posix':
    pytest.skip('requires POSIX ownership, directory descriptors and advisory locks')
  # /tmp is intentionally rejected by the production ancestry check.
  with tempfile.TemporaryDirectory(prefix='.pv5-warp-test-', dir=Path.home()) as directory:
    root = Path(directory)
    monkeypatch.setattr(cache, 'CACHE', root / 'cache')
    calls = []
    key = [hashlib.sha256(b'platform A').digest()]
    monkeypatch.setattr(cache, 'identity', lambda *args: key[0])
    def verify(width, height, blob):
      calls.append(('verify', blob))
      assert blob.startswith(b'local')
    class Warp:
      def __init__(self, *size):
        assert size == (1344, 760)
      def save_prepared(self, target, width, height):
        calls.append(('build', len(calls)))
        target.write_bytes(b'local' + bytes([len(calls)]))
    monkeypatch.setattr(cache, 'verify_cached', verify)
    monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.jetlink.model', NS(Warp=Warp))
    target = tmp_path / 'prepared'
    def run(*, refresh=False):
      target.unlink(missing_ok=True)
      return cache.prepare(1344, 760, target, refresh=refresh)
    yield NS(root=root, target=target, run=run, calls=calls, key=key, verify=verify)


def test_cold_then_warm_then_changed_identity_then_forced_refresh(slot):
  assert slot.run() == 'rebuilt'
  first = slot.target.read_bytes()
  assert slot.run() == 'hit' and slot.target.read_bytes() == first
  slot.key[0] = hashlib.sha256(b'new driver').digest()
  assert slot.run() == 'rebuilt'
  assert slot.run(refresh=True) == 'rebuilt'
  last = slot.target.read_bytes()
  assert last != first and slot.run() == 'hit' and slot.target.read_bytes() == last
  assert [name for name, _ in slot.calls] == ['build', 'verify', 'build', 'build', 'verify']
  assert sorted(p.name for p in cache.CACHE.iterdir()) == sorted((cache.ACTIVE, cache.SEAL))


@pytest.mark.parametrize('fault', ['short', 'oversize', 'modified', 'recomputed_sha', 'foreign_signed', 'permissions',
                                   'hardlink', 'symlink', 'fifo', 'missing_seal', 'bad_seal', 'public_seal', 'interrupted'])
def test_bad_entries_rebuild_before_deserialization(slot, fault, monkeypatch):
  assert slot.run() == 'rebuilt'
  file = cache.CACHE / cache.ACTIVE
  seal = cache.CACHE / cache.SEAL
  if fault == 'short':
    file.write_bytes(b'x')
  elif fault == 'oversize':
    monkeypatch.setattr(cache, 'MAX_ARTIFACT', 8)
    with file.open('ab') as stream:
      stream.write(b'x' * 50)
  elif fault == 'modified':
    with file.open('r+b') as stream:
      stream.seek(-1, 2)
      stream.write(b'x')
  elif fault == 'recomputed_sha':
    file.write_bytes(slot.key[0] + hashlib.sha256(b'forged').digest() + b'forged')
  elif fault == 'foreign_signed':
    seal.write_bytes(os.urandom(32))
  elif fault == 'permissions':
    file.chmod(0o644)
  elif fault == 'hardlink':
    os.link(file, slot.root / 'other')
  elif fault in ('symlink', 'fifo'):
    file.unlink()
    if fault == 'symlink':
      file.symlink_to(slot.target)
    else:
      os.mkfifo(file, 0o600)
  elif fault == 'missing_seal':
    seal.unlink()
  elif fault == 'bad_seal':
    seal.write_bytes(b'')
  elif fault == 'public_seal':
    seal.chmod(0o644)
  elif fault == 'interrupted':
    file.rename(cache.CACHE / cache.CHECKING)
  assert slot.run() == 'rebuilt'
  assert [name for name, _ in slot.calls] == ['build', 'build']
  assert slot.run() == 'hit'


def test_killed_check_is_quarantined_before_execution(slot, monkeypatch):
  assert slot.run() == 'rebuilt'
  def die(*args):
    assert not (cache.CACHE / cache.ACTIVE).exists()
    assert (cache.CACHE / cache.CHECKING).is_file()
    os._exit(73)  # Skip all cleanup just like an externally killed compiler.
  monkeypatch.setattr(cache, 'verify_cached', die)
  child = os.fork()
  if child == 0:
    slot.run()
    os._exit(1)
  _, status = os.waitpid(child, 0)
  assert os.waitstatus_to_exitcode(status) == 73
  assert not (cache.CACHE / cache.ACTIVE).exists() and (cache.CACHE / cache.CHECKING).is_file()
  assert slot.run() == 'rebuilt'
  monkeypatch.setattr(cache, 'verify_cached', slot.verify)
  assert slot.run() == 'hit'


def test_pixel_failure_rebuilds_only_once(slot, monkeypatch):
  assert slot.run() == 'rebuilt'
  def reject(*args):
    raise ValueError('pixel mismatch')
  monkeypatch.setattr(cache, 'verify_cached', reject)
  assert slot.run() == 'rebuilt'
  monkeypatch.setattr(cache, 'verify_cached', slot.verify)
  assert slot.run() == 'hit'
  assert [name for name, _ in slot.calls] == ['build', 'build', 'verify']


@pytest.mark.parametrize('fault', ['cache_mode', 'cache_symlink', 'parent_mode', 'parent_symlink', 'identity', 'write', 'locked'])
def test_optional_cache_failure_still_delivers_valid_cold_capture(slot, monkeypatch, fault):
  if fault == 'cache_mode':
    cache.CACHE.mkdir(mode=0o755)
  elif fault == 'cache_symlink':
    cache.CACHE.symlink_to(slot.root)
  elif fault == 'parent_mode':
    slot.root.chmod(0o777)
  elif fault == 'parent_symlink':
    link = slot.root / 'link'
    link.symlink_to(slot.root)
    monkeypatch.setattr(cache, 'CACHE', link / 'cache')
  elif fault in ('identity', 'write'):
    def fail(*args):
      raise OSError('unavailable')
    monkeypatch.setattr(cache, 'identity' if fault == 'identity' else 'store_entry', fail)
  elif fault == 'locked':
    with cache.private_slot():
      assert slot.run() == 'uncached'
    assert slot.target.read_bytes().startswith(b'local')
    return
  assert slot.run() == 'uncached'
  assert slot.target.read_bytes().startswith(b'local')
