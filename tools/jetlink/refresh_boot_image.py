"""Offline, diagnosis-only refresh of the exact R2 protected SD base image.

Creates a separate PC image, never accepts a physical disk or a provisioned
card. Firmware/GPT/SETUP and all files outside diagnostics and the two factory
panel-lock helpers must remain identical. Output stays private until boot evidence.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile

from install_boot_display import FILES, VENDOR, configure
from refresh_sd_image import audit_unprovisioned, digest

BASE_SHA256 = 'b11f5601d3a713ad0de23315ee90daddf5452f8e548f2c87c8eeec28d321e55f'
IMAGE_BYTES = 40 * (1 << 30)
DIAGNOSTICS = 'usr/local/lib/carrot-jetlink/boot-display'
SERVICE = 'etc/systemd/system/carrot-jetlink-boot-display.service'
INSTALL_PATHS = (SERVICE, 'etc/systemd/system/multi-user.target.wants/carrot-jetlink-boot-display.service',
                 'etc/systemd/system/carrot-jetlink-hud.service.d/diagnostics.conf')


def run(*args):
  subprocess.run([str(a) for a in args], check=True)


def git_command(repository, executable):
  location = str(repository)
  # Windows Git worktree .git pointers are Windows absolute paths. Native WSL
  # Git cannot resolve those; use the owner's Git without rewriting .git.
  if str(executable).endswith('.exe'):
    location = subprocess.check_output(['wslpath', '-w', location], text=True).strip()
  return [str(executable), '-C', location]


def validate_layout(layout, size):
  if size != IMAGE_BYTES or layout.get('label') != 'gpt' or layout.get('sectorsize') != 512:
    raise ValueError('Only the exact protected 40 GiB R2 base is supported')
  parts = layout.get('partitions', [])
  if len(parts) != 17:
    raise ValueError('Expected all 17 original partitions')
  expected = {0: ('APP', 3188736, 47140864), 15: ('CARROT_SETUP', 3057664, 131072),
              16: ('CARROT_DATA', 50331648, 33552384)}
  for index, value in expected.items():
    if tuple(parts[index].get(key) for key in ('name', 'start', 'size')) != value:
      raise ValueError('Protected partition geometry changed')
  return parts[0]['start'] * 512, parts[0]['size'] * 512


def image_digests(image, app_offset, app_size, data_offset=None, data_size=None):
  """Full image plus bytes outside the named ext4 partitions (including GPT)."""
  full, outside = hashlib.sha256(), hashlib.sha256()
  regions = [(app_offset, app_offset + app_size)]
  if data_offset is not None:
    regions.append((data_offset, data_offset + data_size))
  position = 0
  with image.open('rb') as stream:
    while block := stream.read(8 << 20):
      full.update(block)
      cursor = 0
      for low, high in sorted(regions):
        before = min(len(block), max(0, low - position))
        after = min(len(block), max(0, high - position))
        if before > cursor:
          outside.update(block[cursor:before])
        cursor = max(cursor, after)
      outside.update(block[cursor:])
      position += len(block)
  return full.hexdigest(), outside.hexdigest()


def diagnosis_path(name):
  return name == DIAGNOSTICS or name.startswith(DIAGNOSTICS + '/') or name in INSTALL_PATHS


def file_manifest(root, allowed=()):
  """Ignore only the explicit diagnostic files; don't follow image symlinks."""
  manifest = {}
  for directory, subdirs, files in os.walk(root, followlinks=False):
    for name in subdirs + files:
      path = Path(directory) / name
      relative = path.relative_to(root).as_posix()
      if diagnosis_path(relative) or relative in allowed:
        continue
      info = path.lstat()
      if stat.S_ISREG(info.st_mode):
        content = digest(path)
      elif stat.S_ISLNK(info.st_mode):
        content = os.readlink(path)
      else:
        content = None
      manifest[relative] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                            info.st_uid, info.st_gid, content)
    subdirs[:] = [name for name in subdirs if not diagnosis_path((Path(directory) / name).relative_to(root).as_posix())]
  return manifest


def factory_owner(root, runtime):
  """Resolve only a factory release inside this offline filesystem."""
  release = (root / runtime / 'current').resolve(strict=True)
  releases = (root / runtime / 'releases').resolve(strict=True)
  if release.parent != releases or not re.fullmatch('[0-9a-f]{40}', release.name):
    raise ValueError('Unexpected factory release path')
  path = release / 'tools/jetlink/display_owner.py'
  if not path.is_relative_to(root.resolve()) or not stat.S_ISREG(path.lstat().st_mode):
    raise ValueError('Factory panel helper escapes the offline root')
  return path


def manifest_preserved(before, after, allow_install_parents=False):
  if any(after.get(name) != value for name, value in before.items()):
    return False
  parents = {p.as_posix() for name in (*INSTALL_PATHS, DIAGNOSTICS) for p in Path(name).parents if p != Path('.')}
  return all(allow_install_parents and name in parents and value == (stat.S_IFDIR, 0o755, 0, 0, None)
             for name, value in after.items() if name not in before)


def refresh_installations(root, setup, data, source):
  audit_unprovisioned(root, setup)
  if (data / 'identity').exists() or (data / 'setup.json').exists():
    raise ValueError('Provisioned DATA cannot be used as a base')
  app_owner = factory_owner(root, 'opt/carrot-jetlink')
  data_owner = factory_owner(data, 'runtime')
  if digest(app_owner) != digest(data_owner):
    raise ValueError('Factory APP/DATA helper pair disagrees')
  allowed_app = (app_owner.relative_to(root).as_posix(),)
  allowed_data = (data_owner.relative_to(data).as_posix(),)
  before_app = file_manifest(root, allowed_app)
  before_data = file_manifest(data, allowed_data)
  # R2's old HUD imports its own helper. Updating only diagnostics would split
  # ownership across /dev/shm and /run and allow simultaneous USB panel access.
  payload = (source / 'tools/jetlink/display_owner.py').read_bytes()
  old_functions = [ast.dump(node) for node in ast.parse(app_owner.read_text()).body if isinstance(node, ast.FunctionDef)]
  new_functions = [ast.dump(node) for node in ast.parse(payload).body if isinstance(node, ast.FunctionDef)]
  if not old_functions or old_functions != new_functions:
    raise ValueError('Factory helper change exceeds the reviewed lock-directory compatibility fix')
  configure(root, source / 'tools/jetlink')
  for target in (app_owner, data_owner):
    target.write_bytes(payload)
    if target.read_bytes() != payload:
      raise ValueError('Factory panel helper readback failed')
  verify_install(root, source)
  audit_unprovisioned(root, setup)
  if (not manifest_preserved(before_app, file_manifest(root, allowed_app), allow_install_parents=True)
      or not manifest_preserved(before_data, file_manifest(data, allowed_data))):
    raise ValueError('A non-allowlisted APP/DATA file changed')
  return {'non_diagnostic_app_files_unchanged': len(before_app),
          'non_owner_data_files_unchanged': len(before_data),
          'factory_owner_paths': [allowed_app[0], 'CARROT_DATA/' + allowed_data[0]],
          'factory_owner_sha256': hashlib.sha256(payload).hexdigest()}


def verify_install(root, source):
  target = root / DIAGNOSTICS
  for name in FILES:
    if digest(target / name) != digest(source / name):
      raise ValueError('Immutable diagnostic payload mismatch: ' + name)
  if 'ExecStart=/opt/carrot-jetlink/venv/bin/python /' + DIAGNOSTICS not in (root / SERVICE).read_text():
    raise ValueError('Boot service must use immutable diagnostics')
  if '#000000' not in (target / 'tools/jetlink/boot_display.py').read_text():
    raise ValueError('Black recovery screen missing')
  if any(target.rglob('__pycache__')) or any(target.rglob('*.pyc')):
    raise ValueError('Compiled diagnostic state in image')


def build(base, output, repository, git_executable='git'):
  if os.name != 'posix' or os.geteuid() != 0:
    raise RuntimeError('Offline Linux PC root required')
  # lstat rejects physical devices, symlinks and other special files before any write.
  if not stat.S_ISREG(base.lstat().st_mode) or base.stat().st_size != IMAGE_BYTES:
    raise ValueError('A regular pristine image file is required, never a physical disk')
  output = output.absolute()
  if output.exists() or output.parent.resolve() != output.parent:
    raise ValueError('Use a new output directory under a real parent')
  git = git_command(repository, git_executable)
  source_commit = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
  if not re.fullmatch('[0-9a-f]{40}', source_commit):
    raise ValueError('Invalid source commit')
  layout = json.loads(subprocess.check_output(['sfdisk', '--json', str(base)]))['partitiontable']
  app_offset, app_size = validate_layout(layout, base.stat().st_size)
  data_offset, data_size = (layout['partitions'][16][key] * 512 for key in ('start', 'size'))
  print('VERIFYING_PRISTINE_BASE', flush=True)
  original, outside = image_digests(base, app_offset, app_size, data_offset, data_size)
  if original != BASE_SHA256:
    raise ValueError('Base image is not the exact unprovisioned R2 release')
  output.mkdir(mode=0o700)
  source, archive = output / 'source', output / 'source.tar'
  source.mkdir()
  with archive.open('xb') as stream:
    subprocess.run([*git, 'archive', source_commit, '--', *FILES, VENDOR], stdout=stream, check=True)
  with tarfile.open(archive) as bundle:
    bundle.extractall(source, filter='data')
  image = output / 'carrot-jetson-black-boot-CANDIDATE.img'
  print('COPYING_SEPARATE_IMAGE', flush=True)
  run('cp', '--reflink=auto', '--sparse=always', '--', base, image)
  if image.stat().st_nlink != 1 or image.stat().st_ino == base.stat().st_ino:
    raise ValueError('Output image must be independent')
  loop = subprocess.check_output(['losetup', '--find', '--show', '--partscan', str(image)], text=True).strip()
  mounted = []
  try:
    if not re.fullmatch('/dev/loop[0-9]+', loop):
      raise RuntimeError('Unexpected loop device')
    backing = subprocess.check_output(['losetup', '-n', '-O', 'BACK-FILE', loop], text=True).strip()
    if Path(backing).resolve() != image.resolve():
      raise RuntimeError('Unexpected loop backing file')
    run('udevadm', 'settle', '--timeout=10')
    run('blockdev', '--setrw', loop)
    run('blockdev', '--setrw', loop + 'p1')
    run('blockdev', '--setrw', loop + 'p17')
    root, setup, data = output / 'rootfs', output / 'setup', output / 'data'
    root.mkdir()
    setup.mkdir()
    data.mkdir()
    for device, mount, options in ((loop + 'p1', root, 'rw'), (loop + 'p16', setup, 'ro'), (loop + 'p17', data, 'rw')):
      run('mount', '-o', options, device, mount)
      mounted.append(mount)
    audit_unprovisioned(root, setup)
    base_marker = json.loads((root / 'etc/carrot-jetlink-image.json').read_text())
    print('HASHING_UNCHANGED_APP_FILES', flush=True)
    preserved = refresh_installations(root, setup, data, source)
    # Read back and render the files actually installed, with PC dependencies;
    # this is not an ARM/Jetson boot or a USB hardware test.
    code = '''import sys
sys.path.insert(0, sys.argv[1] + '/tools/jetlink')
import boot_display, cluster_usb_display
frame = boot_display.render({'temp_c': 65., 'cpu_percent': 12.3, 'memory_percent': 28.4, 'power_w': 14.7})
assert frame.getpixel((1919, 461)) == (0, 0, 0)
assert len(boot_display.lines({})[1]) == 9
frame.save(sys.argv[2])
'''
    run(os.sys.executable, '-B', '-c', code, root / DIAGNOSTICS, output / 'boot-preview.png')
    for mount in reversed(mounted):
      run('umount', mount)
    mounted.clear()
    run('e2fsck', '-f', '-n', loop + 'p1')
    run('fsck.vfat', '-n', loop + 'p16')
    run('e2fsck', '-f', '-n', loop + 'p17')
  finally:
    for mount in reversed(mounted):
      run('umount', mount)
    run('losetup', '-d', loop)
  run('sgdisk', '-v', image)
  print('VERIFYING_OUTPUT_AND_UNCHANGED_PARTITIONS', flush=True)
  final, final_outside = image_digests(image, app_offset, app_size, data_offset, data_size)
  if outside != final_outside or image.stat().st_size != IMAGE_BYTES:
    raise ValueError('Bytes outside APP/DATA changed')
  result = {'state': 'PROTECTED_CANDIDATE_NOT_BOOT_TESTED', 'source_commit': base_marker['source_commit'],
            'diagnostic_source_commit': source_commit,
            'scope': 'Immutable diagnostics and paired factory panel-lock helpers only',
            'base_sha256': original, 'image': image.name, 'image_bytes': IMAGE_BYTES, 'image_sha256': final,
            'storage_format': 1, 'data_partition': 17, 'outside_app_data_sha256': outside, **preserved,
            'physical_boot_tested': False, 'physical_card_readback': False,
            'validation': 'PC file/partition/privacy/filesystem/import/render checks only; physical boot pending'}
  (output / 'candidate.json').write_text(json.dumps(result, indent=2) + '\n')
  (output / 'SHA256SUMS').write_text(final + '  ' + image.name + '\n')
  print('PRIVATE_BOOT_IMAGE_BUILT', flush=True)
  return result


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--base', type=Path, required=True)
  parser.add_argument('--output-dir', type=Path, required=True)
  parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[2])
  parser.add_argument('--git-executable', default='git')
  args = parser.parse_args()
  build(args.base.absolute(), args.output_dir, args.repository, args.git_executable)
