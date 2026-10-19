"""Negotiate a display-only WLAN side channel once over the existing USB session."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

CAPABILITY = 'carrot_hud_wifi_v1'
REQUEST, RESPONSE = 0x4020, 0x4021
PORT = 7716


class Worker:
  def __init__(self, role, key, script='hud_wifi_worker.py', port=PORT):
    self.socket, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    self.process = None
    try:
      self.process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name(script)), role, str(child.fileno())],
        pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      self.socket.settimeout(2.)
      self.socket.send(json.dumps({'key': key, 'port': port}).encode())
      if self.socket.recv(64) != b'ready':
        raise RuntimeError('HUD Wi-Fi worker did not initialize')
    except BaseException:
      self.close()
      raise
    finally:
      child.close()

  def close(self):
    self.socket.close()
    if self.process is not None:
      try:
        self.process.wait(timeout=1.)
      except subprocess.TimeoutExpired:
        self.process.terminate()
        try:
          self.process.wait(timeout=1.)
        except subprocess.TimeoutExpired:
          self.process.kill()
          self.process.wait(timeout=1.)


class WifiPublisher:
  """No per-frame USB work or WLAN waits in the inference owner."""
  def __init__(self, client):
    self.worker = None
    self.vision = None
    key = secrets.token_hex(32)
    seq = client._next_seq()
    client.t.send_json(REQUEST, seq, {'key': key, 'aux_cpu': True})
    result = json.loads(bytes(client._expect(RESPONSE, seq, 3.).payload))
    if result.get('ready') is not True:
      raise RuntimeError('HUD Wi-Fi receiver unavailable')
    self.worker = Worker('send', key)
    if result.get('aux_cpu') is True:
      try:
        self.vision = Worker('send', key, 'vision_cpu.py', 7717)
      except (OSError, RuntimeError):
        pass  # No auxiliary vision result may interfere with driving model/HUD startup.

  def packet(self):
    return None

  def media_packet(self):
    return None

  def close(self):
    if self.vision is not None:
      self.vision.close()
      self.vision = None
    if self.worker is not None:
      self.worker.close()


def publisher(client, peer, legacy):
  if peer.get('carrot_host') != 'jetson' or peer.get(CAPABILITY) is not True:
    return legacy(navi=bool(peer.get('carrot_navi_v1'))) if peer.get('carrot_hud_v1') else None
  try:
    return WifiPublisher(client)
  except Exception:
    # Do not log the control payload/key or silently put HUD back onto USB.
    import logging
    logging.getLogger(__name__).warning('HUD Wi-Fi setup failed; model link unchanged, reconnect to retry')
    return None


def supported():
  return Path('/etc/nv_tegra_release').is_file() and os.name == 'posix'
