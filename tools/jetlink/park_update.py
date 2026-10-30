"""Negotiated same-model runtime -> empty update-gate handoff, never a live swap.

Only fixed local service actions are available. Package bytes and activation
still use the existing USB receiver, validation, synthetic probe and rollback.
"""
import base64
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time

CAPABILITY = 'carrot_park_update_v1'
REQUEST, RESPONSE = 0x4015, 0x4016
DIRECTORY = Path('/run/carrot-jetlink-display')
REQUEST_FILE = DIRECTORY / 'park-update.json'
RECOVERY = Path('/run/carrot-jetlink-park-update.json')
MAX_BYTES = 65536
LEASE_SECONDS = 1.


def supported():
  import boot_update
  import usb_release
  try:
    return (Path('/etc/nv_tegra_release').is_file() and boot_update.enabled()
            and usb_release.sha256_enabled(boot_update.update.ROOT))
  except (OSError, ValueError, TypeError):
    return False


def validate_request(value, root):
  import update_host as update
  from usb_release import trusted
  from openpilot.common.jetson_model import read_spec
  if not isinstance(value, dict) or set(value) != {'manifest', 'parked'} or value['parked'] is not True:
    raise ValueError('Park update permission missing')
  manifest = value['manifest']
  if not isinstance(manifest, dict) or manifest.get('delivery') != 'usb':
    raise ValueError('Park update requires the selected USB package')
  # Local receiver opt-in, format/URL/size/hash/storage rules remain mandatory.
  trusted(root, manifest)
  update.validate_storage_compatibility(manifest)
  spec = read_spec(root / 'current')
  if any(manifest['model'][key] != spec[key] for key in ('sha256', 'size')):
    raise ValueError('Park update cannot change the boot-selected model')
  if manifest['source_commit'] == (root / 'current/SOURCE_COMMIT').read_text().strip():
    raise ValueError('Runtime is already selected')
  return manifest


def prepare(payload, received, root, clock=time.monotonic):
  if not supported() or not 0 <= clock() - received < LEASE_SECONDS or not 0 < len(payload) <= MAX_BYTES:
    raise ValueError('Park update unavailable or stale')
  value = json.loads(bytes(payload))
  manifest = validate_request(value, root)
  from boot_update import BOOT_ID
  # Private, stable service runtime directory; never use a sticky /tmp name or
  # let a packet supply a command, filesystem path, service or model identity.
  if DIRECTORY.is_symlink() or not DIRECTORY.is_dir():
    raise ValueError('Private display runtime directory unavailable')
  temporary = REQUEST_FILE.with_suffix('.new')
  data = json.dumps({'manifest': manifest, 'received': received, 'boot_id': BOOT_ID.read_text().strip()}).encode()
  if len(data) > MAX_BYTES:
    raise ValueError('Oversized private update request')
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
  with os.fdopen(fd, 'wb') as stream:
    stream.write(data)
  os.replace(temporary, REQUEST_FILE)


def launch(root):
  # The account already has the installed updater's sudo permission. This
  # transient root worker outlives the server which it must cleanly retire.
  subprocess.run(['sudo', '-n', 'systemd-run', '--unit=carrot-jetlink-park-update', '--collect',
      '--property=Restart=on-failure', '--property=RestartSec=3', '--property=Nice=19',
      '--property=CPUAffinity=0 1', str(root / 'venv/bin/python'),
      str(Path(__file__).resolve().with_name('update_host.py')), 'parked-boot'],
      check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def runtime_parked():
  """Independent host-side recheck immediately before retiring live services."""
  from hud_protocol import read_snapshot
  from openpilot.cereal import log
  try:
    received, value = read_snapshot()
    now = time.monotonic()  # Read first; a future timestamp never proves Park.
    names = ('carState', 'carControl', 'selfdriveState')
    if (not 0 <= now - received < .25 or not all(value['valid'][n] and value['alive'][n]
        and 0 <= value['sent'] - value['received'][n] < .25
        and now - received + value['sent'] - value['received'][n] < .25 for n in names)):
      return False
    events = {}
    for name in names:
      raw = base64.b64decode(value['events'][name], validate=True)
      with log.Event.from_bytes(raw) as event:
        if event.which() != name:
          return False
        events[name] = event.as_builder()
    cs = events['carState'].carState
    return (cs.canValid and not cs.canTimeout and str(cs.gearShifter) == 'park'
            and math.isfinite(cs.vEgoRaw) and cs.vEgoRaw == 0.
            and not events['carControl'].carControl.enabled and not events['selfdriveState'].selfdriveState.active)
  except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
    return False


def read_request(path, uid):
  fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
  with os.fdopen(fd, 'rb') as source:
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
      raise ValueError('Invalid private update request')
    data = source.read(MAX_BYTES + 1)
  if len(data) > MAX_BYTES:
    raise ValueError('Oversized private update request')
  return json.loads(data)


def enter():
  """Root-only action, under update_host's single updater lock."""
  import boot_update as boot
  import update_host as update
  import pwd
  os.umask(0o077)
  if not supported():
    raise ValueError('Park update policy is unavailable')
  if update.PROTECTED_MARKER.exists():
    if json.loads(Path('/run/carrot-storage.json').read_text()).get('state') != 'protected':
      raise ValueError('Protected storage unavailable')
  boot_id = boot.BOOT_ID.read_text().strip()
  try:
    recovery = read_request(RECOVERY, 0)
  except FileNotFoundError:
    recovery = None
  if recovery != {'boot_id': boot_id}:
    owner = pwd.getpwnam('jetlink')
    value = read_request(REQUEST_FILE, owner.pw_uid)
    stamp = value.get('received')
    if (value.get('boot_id') != boot_id or type(stamp) not in (int, float)
        or not math.isfinite(stamp) or not 0 <= time.monotonic() - stamp < 3.):
      return  # No runtime was stopped. Let the comma request again in Park.
    validate_request({'manifest': value.get('manifest'), 'parked': True}, update.ROOT)
    if not runtime_parked():
      return
    update.atomic_json(RECOVERY, {'boot_id': boot_id})
    RECOVERY.chmod(0o600)
  # ACK completed synchronously before launching this worker. No pending model
  # response is sacrificed and no fixed sleep widens the Park admission window.
  # Re-entry after worker failure resumes only the empty gate. Never use the
  # remembered manifest as activation permission. New USB approval is required.
  subprocess.run(['systemctl', 'stop', 'carrot-jetlink', 'carrot-jetlink-hud'], check=True, timeout=45)
  for unit in ('carrot-jetlink', 'carrot-jetlink-hud'):
    result = subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=False, timeout=5)
    if result.returncode not in (3, 4):
      raise RuntimeError('Runtime retirement not confirmed')
  boot.READY.unlink(missing_ok=True)
  boot.boot(parked=True)
  RECOVERY.unlink(missing_ok=True)
  REQUEST_FILE.unlink(missing_ok=True)
