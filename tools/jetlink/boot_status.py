"""Small, allowlisted boot diagnostics. Never include profiles, keys or journals."""
import ipaddress
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

STAGE = Path('/run/carrot-boot-stage.json')
UPDATE = Path('/run/carrot-jetlink-boot-update.json')
READY = Path('/run/carrot-jetlink-boot-ready.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
RUNTIME = Path('/opt/carrot-jetlink')
UNITS = ('carrot-protected-storage', 'carrot-image-setup', 'carrot-jetlink-usbc-host',
         'nv', 'nvpmodel', 'carrot-jetlink-performance', 'NetworkManager', 'ssh',
         'carrot-jetlink-wifi', 'carrot-jetlink', 'carrot-jetlink-xorg', 'carrot-jetlink-hud')


def update_status():
  """Only public enums/numbers; never display raw errors, URLs, or journals."""
  value = read_json(UPDATE)
  state = value.get('state')
  if state not in ('checking', 'receiving', 'received', 'waiting_usb', 'downloading',
                   'waiting_internet', 'verifying', 'ready', 'failed'):
    return {}
  result = {'state': state}
  stamp = value.get('updated')
  result['stale'] = (type(stamp) not in (int, float) or not math.isfinite(stamp)
                     or not 0 <= time.monotonic() - stamp < 10)
  if state == 'ready':
    # The producer exits normally after success. Its final record remains valid
    # only for this boot and this installed release, without further heartbeats.
    try:
      ready = read_json(READY)
      if (ready.get('boot_id') == BOOT_ID.read_text().strip()
          and ready.get('source_commit') == (RUNTIME / 'current/SOURCE_COMMIT').read_text().strip()):
        result['stale'] = False
    except OSError:
      pass
  for name in ('free_bytes', 'required_bytes', 'received_bytes', 'total_bytes'):
    number = value.get(name)
    if type(number) is int and 0 <= number <= 16 << 40:
      result[name] = number
  fraction = value.get('fraction')
  if type(fraction) in (int, float) and math.isfinite(fraction) and 0 <= fraction <= 1:
    result['fraction'] = fraction
  if value.get('phase') in ('begin', 'transfer', 'verify'):
    result['phase'] = value['phase']
  if value.get('error_code') in ('insufficient_space', 'permission_denied', 'package_rejected', 'usb_disconnected'):
    result['error_code'] = value['error_code']
  if state == 'failed' and 'error_code' not in result:
    result['error_code'] = 'activation_failed'
  return result


def hud_status():
  value = read_json('/dev/shm/carrot-jetlink-hud-startup.json')
  if value.get('stage') not in ('waiting_runtime', 'loading_renderer', 'waiting_usb_panel',
                                'waiting_hud_data', 'waiting_panel_owner', 'renderer_started', 'failed'):
    return {}
  result = {'stage': value['stage']}
  if value.get('reason') in ('runtime_not_ready', 'storage_not_ready', 'wifi_not_ready', 'hud_snapshot_missing',
                             'device_state_invalid', 'host_health_missing', 'host_health_error'):
    result['reason'] = value['reason']
  return result


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
  from host_health import module_power
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
  if not 0 <= time.monotonic() - network.get('updated', -100) < 6:
    network = {}
  temperatures = []
  for path in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
    try:
      temperature = int(path.read_text()) / 1000
      if -20 <= temperature <= 150:
        temperatures.append(temperature)
    except (OSError, ValueError, TypeError):
      pass
  data = {}
  if read_json('/run/carrot-storage.json').get('state') == 'protected':
    try:
      usage = shutil.disk_usage(RUNTIME)
      data = {'free_bytes': usage.free, 'total_bytes': usage.total}
    except OSError:
      pass
  return {'stage': read_json(STAGE), 'storage': read_json('/run/carrot-storage.json'),
          'update': update_status(), 'hud': hud_status(), 'data': data, 'power_w': module_power(),
          'units': units, 'addresses': addresses[:4], 'wifi': network.get('state', 'waiting'),
          'wifi_reason': network.get('reason', '') if network.get('reason') in
          ('rollback_failed', 'profile_install_failed', 'connect_failed', 'persist_failed', 'network_activation_failed',
           'comma_ap_channel_unavailable', 'comma_ap_waiting_offroad', 'comma_ap_unavailable') else '',
          'uptime': int(time.monotonic()), 'temp_c': max(temperatures, default=None)}


def persist_result(directory, value):
  """Called once at the end of storage boot, on the already guarded setup mount."""
  from persistent_state import durable_write
  # Restrict persisted content even if a caller accidentally supplies raw logs.
  record = {'format': 1, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'stage': str(value.get('stage', 'unknown'))[:48],
            'error': str(value.get('error', ''))[:80]}
  durable_write(Path(directory) / 'BOOT-STATUS.json', json.dumps(record).encode(), 0o600)
