"""Copy approved networking helpers to RAM, retaining immutable boot recovery.

The running network worker must not depend on a mutable current-release symlink.
Only restart this small importer, never NetworkManager or an active WLAN link.
"""
import hashlib
from pathlib import Path
import subprocess

from persistent_state import durable_write

NAMES = ('wifi_apply.py', 'wifi_protocol.py', 'image_first_boot.py', 'persistent_state.py')


def configure(root=Path('/'), source=None, command=subprocess.run):
  root = Path(root)
  source = Path(source) if source is not None else Path(__file__).resolve().parent
  files = {name: (source / name).read_bytes() for name in NAMES}
  version = hashlib.sha256(b''.join(name.encode() + b'\0' + files[name] for name in NAMES)).hexdigest()
  relative = Path('run/carrot-jetlink-network') / version
  target = root / relative
  target.mkdir(parents=True, exist_ok=True)
  if target.is_symlink():
    raise ValueError('Unexpected runtime network directory')
  for name, data in files.items():
    path = target / name
    if path.is_symlink():
      raise ValueError('Unexpected runtime network source')
    if not path.exists() or path.read_bytes() != data:
      durable_write(path, data, 0o644)
  dropin = root / 'run/systemd/system/carrot-jetlink-wifi.service.d/runtime.conf'
  body = ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 /' + relative.as_posix() + '/wifi_apply.py\n'
          'Environment=PYTHONDONTWRITEBYTECODE=1\n').encode()
  if dropin.is_symlink():
    raise ValueError('Unexpected runtime network override')
  if dropin.exists() and dropin.read_bytes() == body:
    return False
  # Do not interrupt an importer in its transactional profile replacement.
  if (root / 'etc/NetworkManager/system-connections/.carrot-wifi-rollback.json').exists():
    raise RuntimeError('Network profile transaction in progress')
  dropin.parent.mkdir(parents=True, exist_ok=True)
  durable_write(dropin, body, 0o644)
  try:
    command(['systemctl', 'daemon-reload'], check=True, timeout=10)
    command(['systemctl', 'try-restart', 'carrot-jetlink-wifi'], check=True, timeout=15)
  except Exception:
    # Next boot still starts the immutable worker. Avoid claiming migration on
    # a failed daemon reload/restart; retry on the next runtime startup.
    dropin.unlink(missing_ok=True)
    try:
      command(['systemctl', 'daemon-reload'], check=True, timeout=10)
      command(['systemctl', 'try-restart', 'carrot-jetlink-wifi'], check=True, timeout=15)
    except Exception:
      pass
    raise
  return True
