"""Offline installation of the pinned, CPU-only diagnostic screen."""
from pathlib import Path
import os
import shutil
import tempfile


def configure_owner(runtime, source, base=Path('/usr/local/lib/carrot-jetlink/boot-display')):
  """Install only diagnosis code/assets on DATA; never remount the base OS."""
  from local_release import owned
  if os.geteuid() != 0:
    raise PermissionError('Install diagnostics from the local root console')
  runtime, source, base = Path(runtime), Path(source), Path(base)
  owned(runtime, directory=True)
  owned(base, directory=True)
  target, backup = runtime / 'boot-display', runtime / 'boot-display.previous'
  for tree in (target, backup):
    if tree.exists() or tree.is_symlink():
      owned(tree, directory=True)
      owned(tree / '.owner-installed')
      if (tree / '.owner-installed').read_text() != '1\n':
        raise ValueError('Unexpected diagnostic installation')
  names = ('boot_display.py', 'boot_status.py', 'host_health.py', 'display_owner.py')
  for name in names:
    if not (source / name).is_file():
      raise ValueError('Incomplete diagnostic source')
  if target.is_dir() and all((target / 'tools/jetlink' / name).is_file()
                             and (target / 'tools/jetlink' / name).read_bytes() == (source / name).read_bytes()
                             for name in names):
    return target  # Reconnects do not copy assets, sync DATA, or flicker the panel.
  with tempfile.TemporaryDirectory(dir=runtime, prefix='boot-display-install-') as temporary:
    staged = Path(temporary) / 'display'
    shutil.copytree(base, staged)
    for name in names:
      destination = staged / 'tools/jetlink' / name
      shutil.copyfile(source / name, destination)
      destination.chmod(0o644)
    (staged / '.owner-installed').write_text('1\n')
    (staged / '.owner-installed').chmod(0o644)
    staged.chmod(0o755)
    os.sync()
    if backup.exists():
      shutil.rmtree(backup)
    if target.exists():
      target.rename(backup)
    try:
      staged.rename(target)
    except Exception:
      if backup.exists() and not target.exists():
        backup.rename(target)
      raise
    os.sync()
  return target


def configure(root, source):
  root, source = Path(root), Path(source)
  repository = source.resolve().parents[1]
  target = root / 'usr/local/lib/carrot-jetlink/boot-display'
  relative = ['openpilot/__init__.py', 'tools/jetlink/boot_display.py', 'tools/jetlink/boot_status.py',
              'tools/jetlink/display_owner.py', 'openpilot/common/usbgpu_bus_lock.py',
              'tools/jetlink/host_health.py',
              'openpilot/selfdrive/carrot/cluster/cluster_usb_display.py',
              'openpilot/selfdrive/carrot/cluster/cluster_config.py',
              'openpilot/selfdrive/carrot/cluster/cluster_utils.py',
              'openpilot/selfdrive/assets/fonts/KaiGenGothicKR-Bold.ttf', 'LICENSE']
  for name in relative:
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repository / name, path)
  vendor = 'openpilot/selfdrive/carrot/cluster/.vendor/turing-smart-screen-python-main'
  shutil.copytree(repository / vendor, target / vendor, dirs_exist_ok=True,
                  ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'log.log'))
  units = root / 'etc/systemd/system'
  units.mkdir(parents=True, exist_ok=True)
  name = 'carrot-jetlink-boot-display.service'
  (units / name).write_text('''[Unit]
Description=Jetson USB boot and recovery status without Xorg or comma
After=local-fs.target systemd-udev-trigger.service
StartLimitIntervalSec=0
[Service]
User=jetlink
SupplementaryGroups=plugdev video render
RuntimeDirectory=carrot-jetlink-display
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
WorkingDirectory=/run/carrot-jetlink-display
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
ExecStart=/opt/carrot-jetlink/venv/bin/python /usr/local/lib/carrot-jetlink/boot-display/tools/jetlink/boot_display.py
Restart=always
RestartSec=3
Nice=19
CPUAffinity=0 1
UMask=0077
[Install]
WantedBy=multi-user.target
''')
  link = units / 'multi-user.target.wants' / name
  link.parent.mkdir(parents=True, exist_ok=True)
  link.unlink(missing_ok=True)
  link.symlink_to('../' + name)
  dropin = units / 'carrot-jetlink-hud.service.d/diagnostics.conf'
  dropin.parent.mkdir(parents=True, exist_ok=True)
  dropin.write_text('[Service]\nRuntimeDirectory=carrot-jetlink-display\n'
                   'RuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n')


if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser(description='Refresh explicitly root-installed boot diagnostics on DATA')
  parser.add_argument('--owner-runtime', type=Path, required=True)
  args = parser.parse_args()
  if not (args.owner_runtime / 'boot-display/.owner-installed').is_file():
    raise RuntimeError('Owner diagnostics must first be installed locally')
  configure_owner(args.owner_runtime, Path(__file__).resolve().parent)
  from boot_update import configure_diagnostic_display
  configure_diagnostic_display()
