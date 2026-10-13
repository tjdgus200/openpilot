"""One-time offline/root provisioning. Never invoked from comma or a USB message."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

from install_local_experiment import atomic_copy
from local_release import owned
from relay_manifest import verify
from update_host import digest

HELPERS = ('finalize_sd_image.py', 'hud_protocol.py', 'wifi_protocol.py', 'local_release.py',
           'relay_manifest.py', 'relay_release.py', 'boot_update.py', 'update_host.py')


def inspect(package, fingerprint):
  public = (package / 'comma-relay-public.pem').read_bytes()
  if hashlib.sha256(public).hexdigest() != fingerprint:
    raise ValueError('Public key fingerprint differs from the PC-provisioned identity')
  manifest = verify(json.loads((package / 'comma_host_release.json').read_text()), public)
  bundle = package / 'host.tar.gz'
  if bundle.stat().st_size != manifest['bundle']['size'] or digest(bundle) != manifest['bundle']['sha256']:
    raise ValueError('Bootstrap bundle identity mismatch')
  with tarfile.open(bundle) as archive:
    if archive.extractfile('SOURCE_COMMIT').read().decode().strip() != manifest['source_commit']:
      raise ValueError('Bootstrap source identity mismatch')
    for name in (*HELPERS, 'install_comma_relay.py', 'install_local_experiment.py', 'release-signing-public.pem'):
      if archive.extractfile('tools/jetlink/' + name).read() != (package / name).read_bytes():
        raise ValueError('Bootstrap helper differs from signed bundle: ' + name)
  return manifest


def install(package, runtime, fingerprint):
  if os.geteuid() != 0:
    raise PermissionError('Offline root provisioning is required')
  package, runtime = Path(package).resolve(), Path(runtime).resolve()
  inspect(package, fingerprint)
  if runtime == Path('/opt/carrot-jetlink').resolve():
    for unit in ('carrot-jetlink', 'carrot-jetlink-hud', 'carrot-jetlink-boot-gate', 'carrot-jetlink-update-stage'):
      if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
        raise RuntimeError('Stop the runtime or use recovery/offline provisioning')
  updater = runtime / 'updater'
  for path in (runtime, updater):
    owned(path, directory=True)
  if not all((runtime / name).is_file() for name in ('protected-runtime.json', 'boot-update-required')):
    raise ValueError('Expected an existing protected runtime with boot gate')
  official = updater / 'release-signing-public.pem'
  owned(official)
  if official.read_bytes() != (package / official.name).read_bytes():
    raise ValueError('Official signing key differs; refusing to replace it')
  trust = runtime / 'trust'
  trust.mkdir(mode=0o755, exist_ok=True)
  owned(trust, directory=True)
  key = trust / 'comma-relay-public.pem'
  if key.exists() or key.is_symlink():
    owned(key)
    if key.read_bytes() != (package / key.name).read_bytes():
      raise ValueError('A different personal key is already provisioned; explicit key rotation is required')
  backup = updater / 'before-comma-relay'
  backup.mkdir(mode=0o700, exist_ok=True)
  owned(backup, directory=True)
  # Restore is possible without touching the official key or current release.
  # Dependency-first copy, root entry point last. Receiver capability appears
  # only after every file and the independently provisioned key are durable.
  for name in ('local_release.py', 'relay_manifest.py', 'relay_release.py', 'boot_update.py', 'update_host.py'):
    target = updater / name
    if target.exists() or target.is_symlink():
      owned(target)
      if not (backup / name).exists():
        atomic_copy(target, backup / name)
    atomic_copy(package / name, target)
  atomic_copy(package / key.name, key)
  os.sync()
  print('COMMA_RELAY_PROVISIONED', fingerprint)
  print('Official key, current runtime, OS, firmware and network settings retained.')


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent)
  parser.add_argument('--runtime', type=Path, required=True)
  parser.add_argument('--key-sha256', required=True)
  args = parser.parse_args()
  install(args.package, args.runtime, args.key_sha256)
