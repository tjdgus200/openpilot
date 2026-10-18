"""Keep USB input current at zero without cutting a USB-only supply.

Adapted from zoompilot/jetlink 491cf1ef's draw policy. Carrot's daemon is always
enabled, so retain the vote across every transport and daemon exit. Only a
verified empty port may change it. Never run sudo on an inference frame.
"""
import logging
import os
from pathlib import Path
import subprocess

log = logging.getLogger('carrot.jetlink')
SCRIPT = Path(__file__).resolve().parents[4] / 'tools/jetlink/usb_power.sh'
POWER_ROLE = Path('/sys/class/usbpd/usbpd0/current_pr')


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
    if role != 'none':
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
