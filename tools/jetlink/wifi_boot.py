"""Early, independent Wi-Fi policy on protected and legacy Jetson images.

Only RAM units/defaults and the existing private profile identity store change.
The immutable recovery importer and model/USB/control service are retained.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from persistent_state import durable_write

RUNTIME = Path('/opt/carrot-jetlink')
TEGRA = Path('/etc/nv_tegra_release')
CONFIG = Path('/run/NetworkManager/conf.d/carrot-jetlink-power.conf')
DEFAULTS = b'[connection-carrot-jetlink-power]\nmatch-device=type:wifi\nwifi.powersave=2\n'
DROPIN = Path('/run/systemd/system/carrot-jetlink-wifi.service.d/runtime-policy.conf')
STATUS = Path('/run/carrot-jetlink-wifi-boot/status.json')
FILES = ('wifi_boot.py', 'wifi_power.py', 'wifi_direct_netlink.py', 'wifi_apply.py',
         'wifi_protocol.py', 'persistent_state.py', 'image_first_boot.py',
         'boot_update.py', 'update_host.py')


def stamp(source):
  return hashlib.sha256(b''.join(name.encode() + b'\0' + (source / name).read_bytes()
                                 for name in FILES)).hexdigest()


def launch(command):
  def work():
    try:
      subprocess.run(command, timeout=10, check=False, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
      pass
  try:
    threading.Thread(target=work, name='carrot-wifi-boot-start', daemon=True).start()
  except RuntimeError:
    pass


def unit(name, runtime, action):
  return ['sudo', '-n', 'systemd-run', '--unit=' + name, '--collect',
          '--property=Type=oneshot', '--property=TimeoutStartSec=35',
          '--property=Nice=19', '--property=CPUSchedulingPolicy=other',
          '--property=CPUAffinity=0 1', '--property=IOSchedulingClass=idle', sys.executable,
          str(runtime / ('updater/wifi_boot.py' if action == 'boot' else 'current/tools/jetlink/wifi_boot.py')),
          action]


def migrate(runtime):
  # No synchronous sudo, sync(), NM query or network wait in the model server.
  try:
    source = runtime / 'current/tools/jetlink'
    if not TEGRA.is_file() or not (source / 'wifi_boot.py').is_file():
      return
    marker = runtime / 'cache/wifi-early-source.sha256'
    version = stamp(source)
    try:
      installed = marker.read_text().strip() == version and stamp(runtime / 'updater') == version
    except OSError:
      installed = False
    if installed:
      return
    launch(unit('carrot-jetlink-wifi-policy-install', runtime, 'install'))
  except (OSError, ValueError):
    pass


def schedule(runtime):
  try:
    if TEGRA.is_file() and (runtime / 'updater/wifi_boot.py').is_file():
      launch(unit('carrot-jetlink-wifi-boot', runtime, 'boot'))
  except OSError:
    pass


def profiles_off(directory):
  """Preserve keyfile bytes/secrets; alter only owned station power settings."""
  import configparser
  import re
  changed = {}
  for path in directory.glob('carrot-usb-*.nmconnection'):
    if path.is_symlink() or path.stat().st_size > 32768:
      raise ValueError('Unexpected managed Wi-Fi profile')
    old = path.read_bytes()
    text = old.decode()
    parsed = configparser.ConfigParser(interpolation=None, strict=False)
    parsed.read_string(text)
    section = next((s for s in ('wifi', '802-11-wireless') if s in parsed), None)
    if section is None or parsed[section].get('mode', 'infrastructure') != 'infrastructure':
      continue
    if parsed[section].get('powersave') == '2':
      continue
    pattern = r'(?ms)(^\[' + re.escape(section) + r'\]\s*\n)(.*?)(?=^\[|\Z)'
    def replace(match):
      body, count = re.subn(r'(?mi)^([ \t]*powersave[ \t]*=)[^\r\n]*', r'\g<1>2', match[2])
      return match[1] + (body if count else 'powersave=2\n' + body)
    new, count = re.subn(pattern, replace, text)
    if count != 1:
      raise ValueError('Ambiguous managed Wi-Fi section')
    changed[path] = (old, new.encode())
  return changed


def install(runtime=RUNTIME):
  from install_updates import bootstrap
  source = runtime / 'current/tools/jetlink'
  version = stamp(source)
  # The USB boot pin is preserved byte-for-byte. Dependencies are copied first;
  # no global sync() or changes to the read-only APP filesystem are needed.
  bootstrap(Path('/'), source, preserve_policy=True, sync=False)
  changed = profiles_off(Path('/etc/NetworkManager/system-connections'))
  backup = runtime / 'updates/wifi-policy-previous'
  try:
    if backup.is_symlink():
      raise ValueError('Unexpected Wi-Fi backup directory')
    for path in backup.glob('carrot-usb-*.nmconnection'):
      if path.is_symlink():
        raise ValueError('Unexpected Wi-Fi backup path')
      if path.name not in {p.name for p in changed}:
        path.unlink()
    for path, (old, new) in changed.items():
      durable_write(backup / path.name, old, 0o600)
      durable_write(path, new, 0o600)
    helper = Path('/usr/lib/carrot-jetlink-storage/protected_storage.py')
    if changed and helper.is_file():
      subprocess.run(['/usr/bin/python3', str(helper), 'save'], check=True, timeout=20,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    durable_write(runtime / 'cache/wifi-early-source.sha256', (version + '\n').encode(), 0o600)
  except Exception:
    for path, (old, _) in changed.items():
      durable_write(path, old, 0o600)
    raise


def prepare(runtime=RUNTIME):
  import time
  from wifi_power import schedule as power
  boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
  body = ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' +
          str(runtime / 'updater/wifi_apply.py') + '\n')
  try:
    previous = json.loads(STATUS.read_text())
    if (previous.get('state') == 'configured' and previous.get('boot_id') == boot_id
        and CONFIG.read_bytes() == DEFAULTS and DROPIN.read_text() == body):
      return  # A restarted USB gate must not restart a healthy Wi-Fi importer.
  except (OSError, ValueError):
    pass
  # Start enforcement before the USB pin/model gate, even without a phone/AP.
  power(runtime)
  durable_write(CONFIG, DEFAULTS, 0o644)
  subprocess.run(['nmcli', 'general', 'reload', 'conf'], check=True, timeout=5,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  # Replace only the recovery importer process with the installed stable copy.
  # NetworkManager, its radio, USB and model services are never restarted here.
  durable_write(DROPIN, body.encode(), 0o644)
  subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=10,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  subprocess.run(['systemctl', 'try-restart', 'carrot-jetlink-wifi.service'], check=True, timeout=15,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  durable_write(STATUS, json.dumps({'state': 'configured', 'updated': time.monotonic(),
                                   'boot_id': boot_id, 'power_default': 2,
                                   'network_worker': 'runtime'}).encode(), 0o644)


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=('install', 'boot'))
  args = parser.parse_args()
  if os.geteuid() != 0 or not TEGRA.is_file():
    raise PermissionError('Requires the Jetson Wi-Fi boot service')
  if args.action == 'install':
    install()
  else:
    prepare()
