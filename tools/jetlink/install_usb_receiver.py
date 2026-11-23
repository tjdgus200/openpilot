"""One-time local enrollment; run against stopped live or recovery-mounted DATA."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from install_local_experiment import atomic_copy
from local_release import owned

FILES = ('finalize_sd_image.py', 'local_release.py', 'usb_release.py', 'boot_update.py', 'update_host.py')
SOURCE = Path(__file__).resolve().parent


def install(runtime, source=SOURCE, *, legacy_image=False):
  if os.geteuid() != 0:
    raise PermissionError('Enroll from the local root/recovery console')
  runtime, source = Path(runtime).resolve(), Path(source).resolve()
  owned(runtime, directory=True)
  updater = runtime / 'updater'
  owned(updater, directory=True)
  owned(runtime / 'protected-runtime.json')
  marker = runtime / 'boot-update-required'
  if not legacy_image or marker.exists() or marker.is_symlink():
    owned(marker)
  files = FILES
  if legacy_image:
    from install_updates import bootstrap_files
    files = tuple(name for name in bootstrap_files(source) if name != 'release-signing-public.pem')
  if runtime == Path('/opt/carrot-jetlink').resolve():
    for unit in ('carrot-jetlink', 'carrot-jetlink-hud', 'carrot-jetlink-boot-gate', 'carrot-jetlink-update-stage'):
      if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
        raise RuntimeError('Stop Jetson inference/update services or use recovery mode')
  official = updater / 'release-signing-public.pem'
  owned(official)
  if official.read_bytes() != (source / official.name).read_bytes():
    raise ValueError('Official trust root differs; refusing enrollment')
  from usb_release import SHA256_POLICY, SHA256_POLICY_VALUE
  policy = updater / SHA256_POLICY
  if policy.exists() or policy.is_symlink():
    owned(policy)
  for name in files:
    if not (source / name).is_file():
      raise ValueError('Incomplete receiver source')
    target = updater / name
    if target.exists() or target.is_symlink():
      owned(target)
  backup = updater / 'before-usb-experiments'
  backup.mkdir(mode=0o700, exist_ok=True)
  owned(backup, directory=True)
  for name in files:
    target = updater / name
    if target.exists():
      atomic_copy(target, backup / name)
    atomic_copy(source / name, target)
  # Publish the locally selected policy last, after all receiver files exist.
  # USB packets cannot enable it; official key bytes are never replaced.
  temporary = policy.with_suffix('.new')
  temporary.write_text(json.dumps(SHA256_POLICY_VALUE, sort_keys=True) + '\n')
  temporary.chmod(0o644)
  os.replace(temporary, policy)
  os.sync()
  print('USB SHA-256 receiver enabled; official key and current release preserved.')


def install_legacy_image(image_root, runtime, source=SOURCE):
  """Install the boot receiver on an offline protected APP/DATA pair."""
  image_root, runtime = Path(image_root).resolve(), Path(runtime).resolve()
  live_runtime = Path('/opt/carrot-jetlink')
  factory_runtime = image_root / 'opt/carrot-jetlink'
  if (image_root == Path('/') or runtime in (live_runtime, factory_runtime)
      or (live_runtime.exists() and runtime.samefile(live_runtime))):
    raise ValueError('Use offline-mounted APP and DATA/runtime paths')
  owned(image_root, directory=True)
  owned(image_root / 'etc/carrot-jetlink-protected.json')
  factory_marker = factory_runtime / 'boot-update-required'
  if factory_marker.exists() or factory_marker.is_symlink():
    raise ValueError('Expected a legacy, marker-free APP runtime')
  systemd = image_root / 'etc/systemd/system'
  apply_unit = systemd / 'carrot-jetlink-update-apply.service'
  owned(apply_unit)
  apply_command = 'ExecStart=/opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/updater/update_host.py activate'
  if apply_command not in apply_unit.read_text().splitlines():
    raise ValueError('Unexpected factory update service')
  # The legacy server and HUD have no built-in wait guard. Keep the marker-free
  # immutable APP fallback available if protected DATA could not be mounted.
  guard = ('[Unit]\nWants=carrot-jetlink-update-apply.service\n'
           'After=carrot-jetlink-update-apply.service\n'
           '[Service]\nTimeoutStartSec=0\n'
           "ExecStartPre=/bin/sh -c 'if [ -f /opt/carrot-jetlink/boot-update-required ]; then "
           'exec /usr/bin/env PYTHONPATH=/opt/carrot-jetlink/updater /opt/carrot-jetlink/venv/bin/python -c '
           '"from boot_update import wait_for_runtime; wait_for_runtime()"; fi\'\n')
  dropins = tuple(systemd / f'{name}.service.d/usb-bootstrap.conf'
                  for name in ('carrot-jetlink', 'carrot-jetlink-hud'))
  for dropin in dropins:
    owned(systemd / dropin.parent.name.removesuffix('.d'))
    if dropin.parent.exists() or dropin.parent.is_symlink():
      owned(dropin.parent, directory=True)
    if dropin.exists() or dropin.is_symlink():
      owned(dropin)
      if dropin.read_text() != guard:
        raise ValueError('Unexpected existing USB boot guard')
  install(runtime, source, legacy_image=True)
  for dropin in dropins:
    dropin.parent.mkdir(exist_ok=True)
    temporary = dropin.with_suffix('.new')
    with temporary.open('x') as file:
      file.write(guard)
    temporary.replace(dropin)
  os.sync()
  marker = runtime / 'boot-update-required'
  if not marker.exists():
    temporary = marker.with_suffix('.new')
    with temporary.open('x') as file:
      file.write('next-boot\n')
    temporary.replace(marker)
  os.sync()


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--runtime', type=Path, required=True)
  parser.add_argument('--sha256', action='store_true', required=True,
                      help='Explicitly authorize physical USB installation using SHA-256/length, without author signatures')
  parser.add_argument('--legacy-image-root', type=Path,
                      help='Offline-mounted protected APP with an older factory updater')
  args = parser.parse_args()
  if args.legacy_image_root is not None:
    install_legacy_image(args.legacy_image_root, args.runtime)
  else:
    install(args.runtime)
