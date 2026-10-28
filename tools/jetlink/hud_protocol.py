"""Read-only display snapshots over a negotiated Jetlink extension."""
import base64
import json
from pathlib import Path
import struct
import socket
import subprocess
import sys
import time
from hud_rate import PARAM as FPS_PARAM, rate, selection, RATES, FrameRate

HUD_MESSAGE = 0x4000
HUD_CAPABILITY = 'carrot_hud_v1'
MAX_HUD_BYTES = 512 * 1024
HUD_PACKET = Path('/dev/shm/carrot-jetlink-hud.packet')
HEADER = struct.Struct('<d')
PUBLISHER_RATE = struct.Struct('<4sB')
RATE_MAGIC = b'HFPS'
SERVICES = ('carState', 'carParams', 'modelV2', 'radarState', 'liveTracks', 'longitudinalPlan',
            'lateralPlan', 'controlsState', 'selfdriveState', 'carControl', 'carOutput', 'deviceState',
            'roadCameraState', 'cameraOdometry', 'liveCalibration', 'livePose', 'drivingModelData',
            'liveDelay', 'liveParameters', 'liveTorqueParameters', 'navInstruction', 'navInstructionCarrot',
            'navRoute', 'carrotMan', 'carrotNavi', 'wideRoadCameraState')
PARAMS = ('ClusterHud', 'ClusterHudDebug', 'JetsonHudFps', 'ClusterHudBrightness', 'ClusterHudOrientation',
          'ClusterHudMirror', 'ClusterHudTheme', 'ClusterHudRadarInfo', 'ClusterHudRadarDisplay',
          'ClusterHudRadarSourceColor', 'ClusterHudCameraViewMode', 'ClusterHudPanelLayout',
          'ClusterHudScreenMode', 'ShowPlotMode', 'LanguageSetting', 'IsMetric', 'ShowDateTime',
          'IsOnroad', 'CarParams', 'CalibrationParams', 'CustomSteerRatio', 'SteerActuatorDelay',
          'LateralAccelerationFactor', 'UsbGpuActive')


class Publisher:
  """Keep cereal decoding, JSON and preview work outside the USB owner's GIL."""
  def __init__(self, navi=False, nav_map=False, rate_control=False):
    import os
    self.media_socket = self.media_process = None
    if navi:
      self.media_socket, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
      self.media_socket.setblocking(False)
      try:
        self.media_process = subprocess.Popen(['chrt', '--other', '0', sys.executable,
          str(Path(__file__).with_name('hud_navi.py')), str(child.fileno())], pass_fds=(child.fileno(),),
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
      finally:
        child.close()
    self.path = Path(f'/dev/shm/carrot-jetlink-display-{os.getpid()}.packet')
    self.last_sent = 0.
    self.target_fps = 10
    self.rate_control = rate_control
    self.process = subprocess.Popen(['chrt', '--other', '0', sys.executable,
                                    str(Path(__file__).with_name('hud_publisher.py')), str(self.path),
                                    *(['--nav-map'] if nav_map else []), *(['--rate-control'] if rate_control else [])],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

  def media_packet(self):
    if self.media_socket is not None:
      from hud_navi import CHUNK, HEADER as MEDIA_HEADER
      try:
        return self.media_socket.recv(CHUNK + MEDIA_HEADER.size)
      except BlockingIOError:
        pass
    return None

  def packet(self):
    try:
      with self.path.open('rb') as f:
        stamp, = HEADER.unpack(f.read(HEADER.size))
        if stamp == self.last_sent or not 0 <= time.monotonic() - stamp < .3:
          return None
        body = f.read(96 * 1024 + PUBLISHER_RATE.size + 1)
      if not 0 < len(body) <= 96 * 1024 + PUBLISHER_RATE.size:
        return None
      if body.startswith(RATE_MAGIC):
        if len(body) <= PUBLISHER_RATE.size:
          return None
        _, fps = PUBLISHER_RATE.unpack_from(body)
        self.target_fps = rate(fps) if getattr(self, 'rate_control', False) else 10
        body = body[PUBLISHER_RATE.size:]
      if len(body) > 96 * 1024:
        return None
      self.last_sent = stamp
      return body
    except (OSError, struct.error):
      return None

  def close(self):
    if self.media_process is not None:
      self.media_process.terminate()
      try:
        self.media_process.wait(timeout=1)
      except subprocess.TimeoutExpired:
        self.media_process.kill()
        self.media_process.wait(timeout=1)
      self.media_socket.close()
    self.process.terminate()
    try:
      self.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
      self.process.kill()
      self.process.wait(timeout=1)
    self.path.unlink(missing_ok=True)
    self.path.with_suffix('.tmp').unlink(missing_ok=True)


class SnapshotBuilder:
  def __init__(self, nav_map=False, rate_control=False):
    from openpilot.cereal import messaging
    from openpilot.common.params import Params
    self.sm = messaging.SubMaster(list(SERVICES))
    self.params = Params()
    self.cached = {}
    self.settings = {}
    self.next_params = self.next_send = 0.
    self.target_fps, self.requested_fps = 10, 10
    self.rate_control = rate_control
    self.host_release = None
    release_file = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/modeld/jetlink/host_release.json'
    from openpilot.common.jetson_model import selected_release
    self.host_release = selected_release(release_file)
    self.camera = None
    from hud_nav_map import NavPreviewPublisher
    self.nav_map = NavPreviewPublisher() if nav_map else None
    from openpilot.selfdrive.carrot.cluster.cluster_system_monitor import NetworkAddressProvider
    self.network = NetworkAddressProvider()
    self.battery = None
    self.battery_retry_at = 0.0

  def close(self):
    if self.battery is not None:
      self.battery.close()
    if self.camera is not None:
      self.camera.close()
    self.network.close()

  def camera_previews(self):
    from openpilot.common.jetlink_status import LINK_STATUS, _fresh, host_label
    now = time.monotonic()
    link = _fresh(LINK_STATUS, now)
    try:
      host_display = (host_label(link.get('peer')) == 'jetSON' and
                      0 <= now - float(link['telemetry_updated']) < 3 and
                      link['telemetry'].get('carrot_hud_connected') is True)
    except (KeyError, TypeError, ValueError):
      host_display = False
    # A host panel can start rendering without a camera preview. Only its
    # fresh output heartbeat starts this worker; no panel means no allocation.
    enabled = ((host_display or self.params.get_int('ClusterHud') == 1) and
               (self.params.get_bool('IsOnroad') or self.params.get_int('ClusterHudDebug') >= 1))
    if enabled and self.camera is None:
      from hud_camera import CameraPublisher
      self.camera = CameraPublisher()
    elif not enabled and self.camera is not None:
      self.camera.close()
      self.camera = None
    return self.camera.latest if self.camera is not None else {}

  def battery_snapshot(self, now):
    try:
      from openpilot.selfdrive.carrot.cluster.cluster_config import (
        CLUSTER_SCREEN_MODE_BATTERY, CLUSTER_SCREEN_MODE_PARAM, normalize_cluster_screen_mode,
      )
      enabled = normalize_cluster_screen_mode(self.params.get(CLUSTER_SCREEN_MODE_PARAM)) == CLUSTER_SCREEN_MODE_BATTERY
      if not enabled and self.battery is None:
        return None
      if not enabled:
        self.battery.close()
        self.battery = None
        return None
      if now < self.battery_retry_at:
        return {'version': 1, 'sampled_at': now, 'status': 'waiting', 'fields': {}}
      if self.battery is None:
        from hud_battery import BatteryPublisher
        self.battery = BatteryPublisher()
      vehicle = self.sm['carParams'].carFingerprint
      return self.battery.update(vehicle, True, now)
    except Exception:
      # Optional battery telemetry must not expire the other HUD services.
      # Retry slowly without forwarding exception text or CAN payloads.
      if self.battery is not None:
        self.battery.close()
      self.battery = None
      self.battery_retry_at = now + 5.0
      return {'version': 1, 'sampled_at': now, 'status': 'waiting', 'fields': {}}


  def packet(self):
    from openpilot.cereal import log
    from openpilot.common.jetlink_status import badge
    now = time.monotonic()
    if now < self.next_send:
      return None
    self.sm.update(0)
    for name in SERVICES:
      if self.sm.updated[name]:
        event = log.Event.new_message(logMonoTime=self.sm.logMonoTime[name], valid=self.sm.valid[name])
        setattr(event, name, self.sm[name])
        self.cached[name] = base64.b64encode(event.to_bytes()).decode()
    if now >= self.next_params:
      self.settings = {}
      for key in PARAMS:
        try:
          value = self.params.get(key)
          if value is not None:
            if isinstance(value, bool):
              value = b'1' if value else b'0'
            self.settings[key] = base64.b64encode(value if isinstance(value, bytes) else str(value).encode()).decode()
        except Exception:
          pass
      self.next_params = now + 1
      try:
        self.requested_fps = RATES[selection(base64.b64decode(self.settings.get(FPS_PARAM, 'MA==')))]
      except (ValueError, TypeError):
        self.requested_fps = 10
    fps = getattr(self, 'requested_fps', 10) if getattr(self, 'rate_control', False) else 10
    current = getattr(self, 'target_fps', 10)
    # SubMaster timestamps arrivals inside update(), after the cadence clock
    # above. Check age after sampling/Params reads: a pre-update clock rejects
    # fresh Park as future-dated and can admit data just beyond the 300ms limit.
    if fps == 10 or fps == current or self.rate_increase_allowed(time.monotonic()):
      self.target_fps = fps
    # Forward the admitted rate, so a pending onroad increase cannot silently
    # increase host rendering before the comma publisher's fresh-P check.
    self.settings[FPS_PARAM] = base64.b64encode(str(RATES.index(self.target_fps)).encode()).decode()
    period = 1. / self.target_fps
    self.next_send = now + period
    device_badge = badge()
    if not hasattr(self, 'source_rate'):
      self.source_rate = FrameRate()
    self.source_rate.frame(now)
    record = {'version': 1, 'sent': now, 'events': self.cached, 'params': self.settings,
              'hud_source_fps': self.source_rate.sample(now),
              **({'jetson_release': self.host_release} if self.host_release is not None else {}),
              'vehicle_network_address': self.network.address(),
              'external_compute_label': device_badge[0] if device_badge and device_badge[1] == 'active' else '',
              'received': self.sm.recv_time, 'mono': self.sm.logMonoTime,
              'valid': self.sm.valid, 'alive': self.sm.alive, 'cameras': self.camera_previews()}
    from hud_signal import signal_packet
    record['signal_display'] = signal_packet(now)
    if self.nav_map is not None:
      preview = self.nav_map.packet(self.settings.get('ClusterHudScreenMode') == 'Nw==')
      if preview is not None:
        record['nav_map_preview'] = preview
    battery = self.battery_snapshot(now)
    if battery is not None:
      record['battery_hud'] = battery
    # SubMaster stamps arrivals inside update(), after the cadence timestamp
    # above. Seal the snapshot after sampling/building so fresh services never
    # look future-dated, and slow optional work still counts toward their age.
    record['sent'] = time.monotonic()
    return encode_snapshot(record)

  def rate_increase_allowed(self, now):
    if self.settings.get('IsOnroad') == 'MA==':
      return True
    car = self.sm['carState']
    return (self.sm.valid['carState'] and self.sm.alive['carState']
            and 0 <= now - self.sm.recv_time['carState'] <= .3
            and str(car.gearShifter) == 'park' and car.vEgo == 0.)


def encode_snapshot(record):
  data = json.dumps(record, separators=(',', ':')).encode()
  if len(data) > 96 * 1024:
    # Optional signal metadata must not evict an existing camera or NAV image.
    record.pop('signal_display', None)
    data = json.dumps(record, separators=(',', ':')).encode()
  if len(data) > 96 * 1024:
    record['cameras'] = {}
    data = json.dumps(record, separators=(',', ':')).encode()
  if len(data) > 96 * 1024:
    record.pop('nav_map_preview', None)
    data = json.dumps(record, separators=(',', ':')).encode()
  if len(data) > 96 * 1024:
    record['events'] = {k:v for k,v in record['events'].items() if k != 'navRoute'}
    data = json.dumps(record, separators=(',', ':')).encode()
  if len(data) > 96 * 1024:
    raise ValueError('HUD snapshot exceeds bounded display allocation')
  return data


_cached_snapshot = None
_next_snapshot_read = 0.


def read_snapshot():
  global _cached_snapshot, _next_snapshot_read
  now = time.monotonic()
  if now < _next_snapshot_read:
    return _cached_snapshot
  _next_snapshot_read = now + .02
  _cached_snapshot = None
  try:
    with HUD_PACKET.open('rb') as f:
      data = f.read(MAX_HUD_BYTES + HEADER.size + 1)
    if len(data) > MAX_HUD_BYTES + HEADER.size:
      return None
    received, = HEADER.unpack_from(data)
    if not 0 <= time.monotonic() - received < .5:
      return None
    value = json.loads(data[HEADER.size:])
    if value.get('version') == 1:
      _cached_snapshot = received, value
      return _cached_snapshot
  except (OSError, ValueError, struct.error):
    pass
  return None
