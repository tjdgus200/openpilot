"""Keep Jetson station power saving off without changing Wi-Fi associations."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from wifi_direct_netlink import Wireless

STATUS = Path('/run/carrot-jetlink-wifi-power/status.json')
INTERVAL = 5.
RETRY = 30.
UNIT = 'carrot-jetlink-wifi-power'
TEGRA = Path('/etc/nv_tegra_release')


def command(runtime):
  return ['sudo', '-n', 'systemd-run', '--unit=' + UNIT, '--collect',
          '--property=Nice=19', '--property=CPUSchedulingPolicy=other',
          '--property=CPUAffinity=0 1', '--property=Restart=on-failure',
          '--property=RestartSec=30', '--property=StartLimitIntervalSec=300',
          '--property=StartLimitBurst=3', '--property=NoNewPrivileges=yes',
          '--property=CapabilityBoundingSet=CAP_NET_ADMIN',
          '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
          '--property=RestrictAddressFamilies=AF_NETLINK AF_UNIX',
          '--property=RuntimeDirectory=' + UNIT,
          '--property=RuntimeDirectoryMode=0755',
          '--property=ReadWritePaths=' + str(STATUS.parent),
          sys.executable, str(runtime / 'current/tools/jetlink/wifi_power.py')]


def schedule(runtime):
  # No root/NetworkManager/netlink wait in the model server. A fixed systemd
  # unit name prevents duplicate workers when the server starts again.
  def launch():
    try:
      subprocess.run(command(runtime), check=False, timeout=10,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
      pass  # Optional networking policy must never prevent GPU service startup.
  try:
    if not TEGRA.is_file() or not (runtime / 'current/tools/jetlink/wifi_power.py').is_file():
      return
    threading.Thread(target=launch, name='carrot-wifi-power-start', daemon=True).start()
  except (OSError, RuntimeError):
    pass  # Includes thread exhaustion; this policy cannot stop model startup.


def enforce(wireless):
  rows = [r for r in wireless.interfaces() if r['type'] == 2 and r['name'].startswith('wl')]
  states = []
  for row in rows:
    before = wireless.power_save(row['index'])
    if before != 0:
      # A removed station index may already belong to a different interface.
      if row not in wireless.interfaces():
        continue
      wireless.set_power_save(row['index'], 0)
    after = wireless.power_save(row['index'])
    if after != 0:
      raise OSError('Station power-save setting not applied')
    states.append({'interface': row['name'], 'power_save': after})
  return states


def publish(state, interfaces, path=STATUS):
  value = {'state': state, 'updated': time.monotonic(), 'interfaces': interfaces}
  temporary = path.with_suffix('.new')
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o644)
  with os.fdopen(fd, 'w') as stream:
    json.dump(value, stream)
  os.replace(temporary, path)


def main():
  if os.geteuid() != 0 or not TEGRA.is_file():
    raise PermissionError('Requires the Jetson Wi-Fi power service')
  stop = threading.Event()
  for kind in (signal.SIGINT, signal.SIGTERM):
    signal.signal(kind, lambda *_: stop.set())
  while not stop.is_set():
    wireless = None
    delay = INTERVAL
    try:
      wireless = Wireless()
      states = enforce(wireless)
      publish('disabled' if states else 'waiting_station', states)
    except (OSError, ValueError, KeyError, TypeError):
      publish('netlink_unavailable', [])  # No credentials or raw driver output.
      delay = RETRY
    finally:
      if wireless is not None:
        wireless.close()
    stop.wait(delay)


if __name__ == '__main__':
  main()
