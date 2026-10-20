"""Keep USB input current at zero without cutting a USB-only supply.

Adapted from zoompilot/jetlink 491cf1ef's draw policy. Carrot's daemon is always
enabled, so retain the vote across every transport and daemon exit. An attached
port may be changed only after the owner confirms independent power. Never run
sudo on an inference frame.
"""
import logging
import os
from pathlib import Path
import subprocess
import stat

log = logging.getLogger('carrot.jetlink')
SCRIPT = Path(__file__).resolve().parents[4] / 'tools/jetlink/usb_power.sh'
POWER_ROLE = Path('/sys/class/usbpd/usbpd0/current_pr')
INDEPENDENT_POWER = Path('/data/carrot-jetlink-power/independent-power')


def independent_power_confirmed():
  """Read an explicit root-owned installation policy, never infer it from USB."""
  try:
    for path in (INDEPENDENT_POWER.parent, INDEPENDENT_POWER):
      info = path.lstat()
      if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
        return False
    return INDEPENDENT_POWER.is_file() and INDEPENDENT_POWER.read_bytes() == b'confirmed\n'
  except OSError:
    return False


class UsbPowerGuard:
  def __init__(self):
    self.enabled = Path('/AGNOS').is_file()
    self.applied = False
    self.failed = False
    self.deferred = False

  def step(self):
    """Called only by the connection loop, before any host/session work."""
    if not self.enabled or self.applied or self.failed:
      return
    try:
      role = POWER_ROLE.read_text().strip()
    except OSError:
      role = None
    if role != 'none' and not independent_power_confirmed():
      if not self.deferred:
        log.warning('Jetlink USB power guard deferred: port attached or unknown; keep independent power and reconnect USB offroad to apply')
        self.deferred = True
      return
    command = ['bash', str(SCRIPT), 'off']
    if os.geteuid() != 0:
      command = ['sudo', '-n', *command]
    try:
      result = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
      self.failed = True
      log.exception('Jetlink USB zero-current guard unavailable; not changing other power controls')
      return
    if result.returncode == 3:
      return  # attachment raced the precheck; root helper made no voter change
    if result.returncode != 0 or result.stdout.strip() != 'disabled':
      self.failed = True
      log.error('Jetlink USB zero-current guard failed: %s', (result.stderr or 'unexpected helper response')[-240:].strip())
      return
    self.applied = True
    log.info('Jetlink USB input current held at zero; independent power remains required')
