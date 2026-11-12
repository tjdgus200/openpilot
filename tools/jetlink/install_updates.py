"""Install the small stable updater into a live host or an unmounted image root."""
from pathlib import Path
import os
import argparse
import re
import subprocess
import sys


def migrate_running_release(runtime=Path('/opt/carrot-jetlink')):
  """A signed runtime can migrate its own updater without per-car SSH setup.

  Published images already grant the jetlink service account passwordless sudo.
  Updater migration takes effect next boot. Power setup runs once before GPU
  backend imports; it never accepts a reboot prompt or starts an observer.
  """
  source = runtime / 'current/tools/jetlink'
  if (source / 'power_setup.py').is_file():
    try:
      subprocess.run(['sudo', '-n', sys.executable, str(source / 'install_updates.py'),
                      '--power-runtime'], check=True, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
      print('Automatic Super setup unavailable: ' + type(exc).__name__, flush=True)
  if Path('/etc/systemd/system/carrot-jetlink-boot-display.service').is_file():
    try:
      migrate_boot_display(runtime)
    except (OSError, subprocess.SubprocessError):
      # Optional diagnosis must not withhold the model/control USB service.
      print('Boot display refresh deferred; retaining existing diagnostics', flush=True)
  if not (runtime / 'current/tools/jetlink/boot_update.py').is_file():
    return
  if (runtime / 'boot-update-required').is_file():
    # An enrolled host also needs new protocol/reboot barriers at its next boot.
    # Refresh from the validated current release, never restart the live updater.
    try:
      source = runtime / 'current/tools/jetlink'
      if any(not (runtime / 'updater' / name).is_file()
             or (runtime / 'updater' / name).read_bytes() != (source / name).read_bytes()
             for name in bootstrap_files(source)):
        subprocess.run(['sudo', '-n', sys.executable, str(runtime / 'current/tools/jetlink/install_updates.py'),
                        '--refresh-bootstrap'], check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
      print('Updater refresh deferred; retaining installed bootstrap', flush=True)
    return
  subprocess.run(['sudo', '-n', sys.executable, str(runtime / 'current/tools/jetlink/install_updates.py'),
                  '--bootstrap-only'], check=True, timeout=30)


BOOT_DISPLAY_DROPIN = Path('/run/systemd/system/carrot-jetlink-boot-display.service.d/runtime.conf')
BOOT_DISPLAY_OVERRIDE = """[Service]
ExecStart=
ExecStart=/opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/current/tools/jetlink/boot_display.py
"""


def migrate_boot_display(runtime):
  # Keep the immutable recovery screen for early boot/storage failures. Once
  # the signed runtime is available, both screens must use its stable /run lock.
  try:
    if BOOT_DISPLAY_DROPIN.read_text() == boot_display_override(runtime):
      return
  except OSError:
    pass
  subprocess.run(['sudo', '-n', sys.executable, str(runtime / 'current/tools/jetlink/install_updates.py'),
                  '--boot-display-runtime'], check=True, timeout=30)


def boot_display_override(runtime=Path('/opt/carrot-jetlink')):
  try:
    commit = (runtime / 'current/SOURCE_COMMIT').read_text().strip()
  except OSError:
    commit = ''
  # Refresh only when the selected runtime changes, not on every USB reconnect.
  return BOOT_DISPLAY_OVERRIDE + (f'Environment=CARROT_BOOT_DISPLAY_SOURCE={commit}\n'
                                 if re.fullmatch('[0-9a-f]{40}', commit) else '')


def configure_runtime_boot_display(runtime=Path('/opt/carrot-jetlink')):
  BOOT_DISPLAY_DROPIN.parent.mkdir(parents=True, exist_ok=True)
  temporary = BOOT_DISPLAY_DROPIN.with_suffix('.new')
  temporary.write_text(boot_display_override(runtime))
  temporary.replace(BOOT_DISPLAY_DROPIN)
  subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=10)
  subprocess.run(['systemctl', 'restart', 'carrot-jetlink-boot-display'], check=True, timeout=15)


def bootstrap_files(source):
  names = ['finalize_sd_image.py', 'hud_protocol.py', 'release-signing-public.pem',
           'wifi_protocol.py', 'local_release.py', 'boot_update.py', 'update_host.py']
  # Older offline patch payloads do not include the optional USB receiver.
  # Copying its code never enables the locally selected USB policy.
  if (source / 'usb_release.py').is_file():
    names.insert(-2, 'usb_release.py')
  if (source / 'power_setup.py').is_file():
    names.insert(-2, 'power_setup.py')
  return names


def bootstrap(root, source, defer_this_boot=False, preserve_policy=False):
  root, source = Path(root), Path(source)
  target = root / 'opt/carrot-jetlink/updater'
  target.mkdir(parents=True, exist_ok=True)
  # Dependencies first, entry point last; enable only after every file is durable.
  names = bootstrap_files(source)
  for name in names:
    temporary = target / (name + '.new')
    with temporary.open('wb') as stream:
      stream.write((source / name).read_bytes())
      stream.flush()
      os.fsync(stream.fileno())
    temporary.chmod(0o644)
    temporary.replace(target / name)
  marker = target.parent / 'boot-update-required'
  if not preserve_policy or not marker.is_file():
    temporary = marker.with_suffix('.new')
    temporary.write_text(Path('/proc/sys/kernel/random/boot_id').read_text() if defer_this_boot else 'next-boot\n')
    temporary.replace(marker)
  if hasattr(os, 'sync'):
    os.sync()


def configure(root, source):
  root = Path(root)
  bootstrap(root, source)
  if (Path(source) / 'power_setup.py').is_file():
    from power_setup import configure as configure_power
    configure_power(root, source)
  systemd = root / 'etc/systemd/system'
  command = '/opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/updater/update_host.py'
  units = {
    'carrot-jetlink-update-apply.service': f'''[Unit]
Description=Validate and activate staged Jetson release before inference
After=local-fs.target carrot-image-setup.service
Before=carrot-jetlink.service carrot-jetlink-hud.service
[Service]
Type=oneshot
ExecStart={command} activate
TimeoutStartSec=1000
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
''',
    'carrot-jetlink-update-stage.service': f'''[Unit]
Description=Stage verified Jetson updates when C4 reports offroad
After=network-online.target
[Service]
Type=oneshot
ExecStart={command} automatic
Nice=19
IOSchedulingClass=idle
CPUAffinity=0 1
TimeoutStartSec=1800
''',
    'carrot-jetlink-update-stage.timer': '''[Unit]
Description=Check for Jetson runtime updates while offroad
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=30
[Install]
WantedBy=timers.target
''',
  }
  for name, body in units.items():
    (systemd / name).write_text(body)
  for name, target_name in (('carrot-jetlink-update-apply.service', 'multi-user.target'),
                            ('carrot-jetlink-update-stage.timer', 'timers.target')):
    link = systemd / (target_name + '.wants') / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to('../' + name)


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--boot-display-runtime', action='store_true', help='Use the signed display runtime for this boot only')
  parser.add_argument('--bootstrap-only', action='store_true', help='Migrate an installed runtime; enable from its next boot without OS writes')
  parser.add_argument('--refresh-bootstrap', action='store_true', help='Refresh enrolled bootstrap without changing this boot policy')
  parser.add_argument('--power-runtime', action='store_true', help='Select supported Super mode once at startup without OS writes')
  args = parser.parse_args()
  if args.power_runtime:
    from power_setup import run_once
    run_once()
  elif args.boot_display_runtime:
    configure_runtime_boot_display()
  elif args.bootstrap_only or args.refresh_bootstrap:
    source = Path('/opt/carrot-jetlink/current/tools/jetlink')
    if not (source / 'boot_update.py').is_file():
      raise RuntimeError('Install the boot-gated source release before enabling this policy')
    bootstrap(Path('/'), source, defer_this_boot=True, preserve_policy=args.refresh_bootstrap)
  else:
    configure(Path('/'), Path(__file__).resolve().parent)
