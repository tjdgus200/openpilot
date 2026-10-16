"""One-time local enrollment; run against stopped live or recovery-mounted DATA."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess

from install_local_experiment import atomic_copy
from local_release import owned

FILES = ('finalize_sd_image.py', 'local_release.py', 'usb_release.py', 'boot_update.py', 'update_host.py')
SOURCE = Path(__file__).resolve().parent


def install(runtime, public_key, source=SOURCE):
  from Crypto.PublicKey import ECC
  if os.geteuid() != 0:
    raise PermissionError('Enroll from the local root/recovery console')
  runtime, source, public_key = Path(runtime).resolve(), Path(source).resolve(), Path(public_key).resolve()
  key = ECC.import_key(public_key.read_text())
  if key.has_private() or key.curve != 'Ed25519':
    raise ValueError('Enroll only an Ed25519 public key, never a private key')
  owned(runtime, directory=True)
  updater = runtime / 'updater'
  owned(updater, directory=True)
  for marker in ('boot-update-required', 'protected-runtime.json'):
    owned(runtime / marker)
  if runtime == Path('/opt/carrot-jetlink').resolve():
    for unit in ('carrot-jetlink', 'carrot-jetlink-hud', 'carrot-jetlink-boot-gate', 'carrot-jetlink-update-stage'):
      if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
        raise RuntimeError('Stop Jetson inference/update services or use recovery mode')
  official = updater / 'release-signing-public.pem'
  owned(official)
  if official.read_bytes() != (source / official.name).read_bytes():
    raise ValueError('Official trust root differs; refusing enrollment')
  destination = updater / 'usb-experiment-public.pem'
  if destination.exists() or destination.is_symlink():
    owned(destination)
    if ECC.import_key(destination.read_text()).export_key(format='DER') != key.export_key(format='DER'):
      raise ValueError('A different experiment key is enrolled; explicit local key rotation required')
  for name in FILES:
    if not (source / name).is_file():
      raise ValueError('Incomplete receiver source')
  backup = updater / 'before-usb-experiments'
  backup.mkdir(mode=0o700, exist_ok=True)
  owned(backup, directory=True)
  for name in FILES:
    target = updater / name
    if target.exists():
      owned(target)
      if not (backup / name).exists():
        atomic_copy(target, backup / name)
    atomic_copy(source / name, target)
  # Key last: incomplete installations cannot advertise USB experiment support.
  atomic_copy(public_key, destination)
  os.sync()
  print('USB receiver enrolled; official key and current release preserved.')
  print('Experiment key SHA256:', hashlib.sha256(key.export_key(format='DER')).hexdigest())


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--runtime', type=Path, required=True)
  parser.add_argument('--public-key', type=Path, required=True)
  args = parser.parse_args()
  install(args.runtime, args.public_key)
