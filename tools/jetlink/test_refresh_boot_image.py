import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from refresh_boot_image import (IMAGE_BYTES, DIAGNOSTICS, SERVICE, factory_owner, file_manifest,
                                image_digests, refresh_installations, validate_layout, verify_install)


def layout():
  parts = [{} for _ in range(17)]
  for index, values in {0: ('APP', 3188736, 47140864), 15: ('CARROT_SETUP', 3057664, 131072),
                        16: ('CARROT_DATA', 50331648, 33552384)}.items():
    parts[index] = dict(zip(('name', 'start', 'size'), values, strict=True))
  return {'label': 'gpt', 'sectorsize': 512, 'partitions': parts}


@pytest.mark.parametrize('field,value', [('label', 'dos'), ('sectorsize', 4096), ('partitions', [])])
def test_reject_other_image_formats(field, value):
  bad = layout()
  bad[field] = value
  with pytest.raises(ValueError):
    validate_layout(bad, IMAGE_BYTES)


def test_r2_geometry_and_size_are_not_general_disk_support():
  assert validate_layout(layout(), IMAGE_BYTES) == (3188736 * 512, 47140864 * 512)
  with pytest.raises(ValueError):
    validate_layout(layout(), IMAGE_BYTES - 512)
  for index in (0, 15, 16):
    bad = copy.deepcopy(layout())
    bad['partitions'][index]['size'] -= 1
    with pytest.raises(ValueError):
      validate_layout(bad, IMAGE_BYTES)


def test_every_non_app_byte_is_covered(tmp_path):
  image = tmp_path / 'image'
  # Both boundaries cross read chunks, including partial head/tail blocks.
  original = bytearray(b'Z' * ((20 << 20) + 73))
  image.write_bytes(original)
  offset, size = (8 << 20) - 17, (8 << 20) + 33
  initial = image_digests(image, offset, size)
  original[offset + 3] = ord('A')
  image.write_bytes(original)
  within = image_digests(image, offset, size)
  assert initial[0] != within[0] and initial[1] == within[1]
  original[offset - 1] = ord('B')
  image.write_bytes(original)
  assert image_digests(image, offset, size)[1] != initial[1]
  original[offset - 1] = ord('Z')
  original[offset + size] = ord('C')
  image.write_bytes(original)
  assert image_digests(image, offset, size)[1] != initial[1]


def test_data_exception_does_not_exclude_setup_firmware_or_gaps(tmp_path):
  image = tmp_path / 'image'
  value = bytearray(b'Z' * 100)
  image.write_bytes(value)
  original = image_digests(image, 20, 30, 70, 20)[1]
  for index in (20, 49, 70, 89):
    changed = value[:]
    changed[index] = ord('A')
    image.write_bytes(changed)
    assert image_digests(image, 20, 30, 70, 20)[1] == original
  for index in (0, 19, 50, 69, 90, 99):
    changed = value[:]
    changed[index] = ord('A')
    image.write_bytes(changed)
    assert image_digests(image, 20, 30, 70, 20)[1] != original


def test_only_allowlisted_diagnosis_changes_are_excluded(tmp_path):
  payload = tmp_path / DIAGNOSTICS
  payload.mkdir(parents=True)
  (payload / 'screen.py').write_text('old')
  guard = tmp_path / 'etc'
  guard.mkdir()
  (guard / 'fstab').write_text('root readonly')
  initial = file_manifest(tmp_path)
  (payload / 'screen.py').write_text('new')
  assert file_manifest(tmp_path) == initial
  (guard / 'fstab').write_text('changed')
  assert file_manifest(tmp_path) != initial


@pytest.mark.skipif(sys.platform != 'linux', reason='Offline image uses POSIX symlinks')
def test_factory_helper_cannot_escape_image_or_release_store(tmp_path):
  root = tmp_path / 'image'
  runtime = root / 'runtime'
  release = runtime / 'releases' / ('a' * 40)
  (release / 'tools/jetlink').mkdir(parents=True)
  helper = release / 'tools/jetlink/display_owner.py'
  helper.write_text('original')
  (runtime / 'current').symlink_to('releases/' + 'a' * 40)
  assert factory_owner(root, 'runtime') == helper
  helper.unlink()
  helper.symlink_to('/etc/hosts')
  with pytest.raises(ValueError, match='escapes'):
    factory_owner(root, 'runtime')
  (runtime / 'current').unlink()
  (runtime / 'current').symlink_to('/tmp')
  with pytest.raises(ValueError, match='release path'):
    factory_owner(root, 'runtime')


@pytest.mark.skipif(sys.platform != 'linux', reason='Offline image uses POSIX symlinks')
def test_install_readback_detects_corruption_and_keeps_immutable_exec(tmp_path):
  from install_boot_display import configure
  source = Path(__file__).resolve().parents[2]
  configure(tmp_path, source / 'tools/jetlink')
  verify_install(tmp_path, source)
  assert '/opt/carrot-jetlink/current/' not in (tmp_path / SERVICE).read_text()
  (tmp_path / DIAGNOSTICS / 'tools/jetlink/host_health.py').write_text('corrupt')
  with pytest.raises(ValueError, match='payload mismatch'):
    verify_install(tmp_path, source)


@pytest.mark.skipif(sys.platform != 'linux', reason='Offline APP/DATA symlink contract')
def test_both_factory_huds_share_diagnosis_lock_without_changing_model_files(tmp_path):
  source = Path(__file__).resolve().parents[2]
  root, setup, data = [tmp_path / name for name in ('app', 'setup', 'data')]
  (root / 'etc').mkdir(parents=True)
  (root / 'etc/machine-id').write_text('')
  (root / 'etc/shadow').write_text('root:!:::::::\njetlink:!:::::::\n')
  setup.mkdir()
  original = (source / 'tools/jetlink/display_owner.py').read_text().replace(
    "DIRECTORY = Path('/run/carrot-jetlink-display')", "DIRECTORY = Path('/dev/shm/legacy-panel')")
  helpers = []
  for filesystem, runtime in ((root, 'opt/carrot-jetlink'), (data, 'runtime')):
    release = filesystem / runtime / 'releases' / ('a' * 40)
    (release / 'tools/jetlink').mkdir(parents=True)
    (filesystem / runtime / 'current').symlink_to('releases/' + 'a' * 40)
    helper = release / 'tools/jetlink/display_owner.py'
    helper.write_text(original)
    helpers.append(helper)
    (release / 'model.onnx').write_bytes(b'model unchanged')
  result = refresh_installations(root, setup, data, source)
  pinned = (root / DIAGNOSTICS / 'tools/jetlink/display_owner.py').read_bytes()
  assert all(helper.read_bytes() == pinned for helper in helpers)
  assert len(result['factory_owner_paths']) == 2
  assert 'RuntimeDirectory=carrot-jetlink-display' in (root / SERVICE).read_text()
  assert all(helper.parents[2].joinpath('model.onnx').read_bytes() == b'model unchanged' for helper in helpers)
  # Future helper behavior changes require a new compatibility review.
  helpers[0].write_text(original.replace('pid <= 1', 'pid <= 100'))
  helpers[1].write_bytes(helpers[0].read_bytes())
  with pytest.raises(ValueError, match='exceeds the reviewed'):
    refresh_installations(root, setup, data, source)
