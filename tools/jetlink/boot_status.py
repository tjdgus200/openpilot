"""Small, allowlisted boot diagnostics. Never include profiles, keys or journals."""
import ipaddress
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

STAGE = Path('/run/carrot-boot-stage.json')
UNITS = ('carrot-protected-storage', 'carrot-image-setup', 'carrot-jetlink-usbc-host',
         'nv', 'nvpmodel', 'carrot-jetlink-performance', 'NetworkManager', 'ssh',
         'carrot-jetlink-wifi', 'carrot-jetlink', 'carrot-jetlink-xorg', 'carrot-jetlink-hud')


def update_status(now):
  value = read_json('/run/carrot-jetlink-boot-update.json')
  stamp = value.get('updated')
  if type(stamp) not in (int, float) or not 0 <= now - stamp < 10:
    return {}
  states = {'checking', 'checking_usb', 'transferring', 'downloading', 'verifying',
            'waiting_internet', 'ready', 'failed'}
  state = value.get('state')
  if state not in states:
    return {}
  error = str(value.get('error') or '').lower()
  # Categories only: neither the diagnostic screen nor its status object
  # receives arbitrary exception text, credentials or update URLs.
  reason = ('validation_failed' if 'validation' in error or 'probe' in error else
            'authentication' if any(x in error for x in ('signature', 'permission', 'auth')) else
            'storage' if any(x in error for x in ('space', 'storage')) else
            'network' if state == 'waiting_internet' else 'update_failed' if error else '')
  fraction = value.get('fraction')
  fraction = fraction if type(fraction) in (int, float) and math.isfinite(fraction) and 0 <= fraction <= 1 else None
  return {'state': state, 'fraction': fraction, 'reason': reason}


def read_json(path):
  try:
    with Path(path).open() as stream:
      value = json.loads(stream.read(32769))
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


def stage(name, error=None):
  value = {'stage': name, 'updated': time.monotonic()}
  if error:
    value['error'] = type(error).__name__
  temporary = STAGE.with_suffix('.tmp')
  temporary.write_text(json.dumps(value))
  temporary.replace(STAGE)


def collect():
  units = {}
  try:
    result = subprocess.run(['systemctl', 'show', '--property=Id,ActiveState,SubState,Result',
                             *[name + '.service' for name in UNITS]],
                            capture_output=True, text=True, timeout=3)
    for section in result.stdout.strip().split('\n\n'):
      fields = dict(line.split('=', 1) for line in section.splitlines() if '=' in line)
      name = fields.get('Id', '').removesuffix('.service')
      if name in UNITS:
        units[name] = {key: fields.get(key, 'unknown') for key in ('ActiveState', 'SubState', 'Result')}
  except (OSError, subprocess.SubprocessError):
    pass
  addresses = []
  try:
    result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'scope', 'global'],
                            capture_output=True, text=True, timeout=2, check=True)
    for interface in json.loads(result.stdout):
      for entry in interface.get('addr_info', []):
        address = ipaddress.ip_address(entry.get('local', ''))
        if not address.is_link_local and not address.is_loopback:
          addresses.append(str(address))
  except (OSError, ValueError, subprocess.SubprocessError):
    pass
  network = read_json('/dev/shm/carrot-jetlink-network-status.json')
  stamp = network.get('updated')
  if type(stamp) not in (int, float) or not 0 <= time.monotonic() - stamp < 6:
    network = {}
  temperatures = []
  for path in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
    try:
      temperature = int(path.read_text()) / 1000
      if -20 <= temperature <= 150:
        temperatures.append(temperature)
    except (OSError, ValueError, TypeError):
      pass
  try:
    disk = shutil.disk_usage('/opt/carrot-jetlink')
    storage_free = f'{disk.free / (1 << 30):.1f} / {disk.total / (1 << 30):.1f} GiB'
  except OSError:
    storage_free = '--'
  spec = read_json('/opt/carrot-jetlink/current/openpilot/selfdrive/modeld/jetlink/jetson_model.json')
  sha = spec.get('sha256', '')
  model = ('N16 Nav' if sha == '01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a' else
           'Flip Flop' if sha == '35976b11be3904eb78e32f7dc55a6abf8e9433ac5aa391a1998f1e9ce001b199' else '--')
  return {'stage': read_json(STAGE), 'storage': read_json('/run/carrot-storage.json'),
          'units': units, 'addresses': addresses[:4], 'wifi': network.get('state', 'waiting'),
          'update': update_status(time.monotonic()), 'model': model, 'storage_free': storage_free,
          'uptime': int(time.monotonic()), 'temp_c': max(temperatures, default=None)}


def persist_result(directory, value):
  """Called once at the end of storage boot, on the already guarded setup mount."""
  from persistent_state import durable_write
  # Restrict persisted content even if a caller accidentally supplies raw logs.
  record = {'format': 1, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'stage': str(value.get('stage', 'unknown'))[:48],
            'error': str(value.get('error', ''))[:80]}
  durable_write(Path(directory) / 'BOOT-STATUS.json', json.dumps(record).encode(), 0o600)
