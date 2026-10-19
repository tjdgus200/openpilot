"""Disable Wi-Fi power saving without reconnecting or restarting networking.

NetworkManager applies this default to future connections; saved Wi-Fi profiles
are explicit too. The signed Jetson runtime reapplies it at startup because its
/etc and /run are volatile. No polling thread or inference-path work is added.
"""
import os
from pathlib import Path
import re
import subprocess

from persistent_state import durable_write
from wifi_direct_netlink import Wireless

DEFAULT = Path('/run/NetworkManager/conf.d/90-carrot-wifi-awake.conf')
CONFIG = b'[connection]\nwifi.powersave=2\n'


def nm(*args):
  result = subprocess.run(['nmcli', '--escape', 'no', *args], capture_output=True,
                          text=True, timeout=10, env=dict(os.environ, LC_ALL='C'))
  if result.returncode:
    raise RuntimeError('Wi-Fi power policy operation failed')  # No profile names/secrets.
  return result.stdout.strip()


def configure(command=nm, wireless=None, config=DEFAULT):
  if os.geteuid() != 0:
    raise PermissionError('Wi-Fi power policy requires root')
  if config.is_symlink():
    raise ValueError('Unexpected Wi-Fi power policy path')
  if not config.exists() or config.read_bytes() != CONFIG:
    durable_write(config, CONFIG, 0o644)
  # Reload defaults only: no NetworkManager/service restart or link bounce.
  # Retry even if a previous reload failed after its file was written.
  command('general', 'reload', 'conf')
  changed = 0
  for row in command('-t', '-f', 'UUID,TYPE', 'connection', 'show').splitlines():
    uid, _, kind = row.partition(':')
    if kind not in ('wifi', '802-11-wireless'):
      continue
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', uid):
      raise ValueError('Unexpected Wi-Fi profile identity')
    current = command('-g', '802-11-wireless.powersave', 'connection', 'show', 'uuid', uid)
    if current not in ('2', '2 (disable)', 'disable'):
      command('connection', 'modify', 'uuid', uid, '802-11-wireless.powersave', '2')
      changed += 1
  owned = wireless is None
  wireless = Wireless() if owned else wireless
  try:
    for interface in wireless.interfaces():
      # P2P discovery devices are not connected station/AP data interfaces.
      if interface['type'] in (2, 3) and wireless.power_save(interface['index']) != 0:
        wireless.set_power_save(interface['index'], 0)
  finally:
    if owned:
      wireless.close()
  return changed


if __name__ == '__main__':
  configure()
