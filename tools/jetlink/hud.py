"""Run the existing Carrot renderer using read-only USB-forwarded cereal data."""
import base64
import json
import os
from pathlib import Path
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'openpilot/selfdrive/carrot/cluster'))
from hud_protocol import read_snapshot, SERVICES


def require_usb_display(scan, expected):
  product = scan(expected)
  if product is None:
    raise RuntimeError('Jetson USB display disappeared before opening; retrying')
  return product


def wait_for_usb_display(scan, expected, sleep=time.sleep):
  announced = False
  while scan(expected) is None:
    if not announced:
      print('Waiting for Jetson USB display', flush=True)
      announced = True
    sleep(1.)


class DisplayParams:
  """Only this renderer sees this adapter; no vehicle Params can be written."""
  def __init__(self, *args, **kwargs):
    self.next_read = 0.
    self.values = {}

  def get(self, key, *args, **kwargs):
    # This adapter exists only inside the Jetson renderer. The saved switch
    # controls the vehicle's USB panel, not a panel attached to this host.
    if key == 'ClusterHud':
      return b'1'
    now = time.monotonic()
    if now >= self.next_read:
      record = read_snapshot()
      # Missing telemetry is not an ignition-off command. Keep the last
      # settings until a fresh C4 snapshot explicitly changes them; SubMaster
      # still invalidates all vehicle data immediately on snapshot loss.
      if record is not None:
        self.values = record[1]['params']
      self.next_read = now + .1
    value = self.values.get(key)
    return base64.b64decode(value) if value is not None else None

  def get_bool(self, key, *args, **kwargs):
    return self.get(key) in (b'1', b'True')

  def external_compute_label(self):
    snapshot = read_snapshot()
    label = snapshot[1].get('external_compute_label', '') if snapshot else ''
    return label if label in ('jetSON', 'MAC', 'Jetlink') else ''

  def get_int(self, key, *args, **kwargs):
    try:
      return int(self.get(key) or 0)
    except ValueError:
      return 0

  def get_float(self, key, *args, **kwargs):
    try:
      return float(self.get(key) or 0)
    except ValueError:
      return 0.

  def put_bool_nonblocking(self, key, value):
    if key == 'ClusterHudConnected':
      Path('/dev/shm/carrot-jetlink-hud-connected').write_text(str(bool(value)))


class RemoteSubMaster:
  def __init__(self, services):
    from openpilot.cereal import log
    self.log = log
    self.services = services
    self.data = {}
    self.updated = dict.fromkeys(services, False)
    self.valid = dict.fromkeys(services, False)
    self.alive = dict.fromkeys(services, False)
    self.recv_time = dict.fromkeys(services, 0.)
    self.logMonoTime = dict.fromkeys(services, 0)
    self.generations = {}
    self.last_received = 0.
    for name in services:
      self.data[name] = log.Event.new_message(**{name: {}}) if name != 'liveTracks' else log.Event.new_message(liveTracks={})

  def __getitem__(self, key):
    return getattr(self.data[key], key)

  def update(self, timeout=0):
    self.updated = dict.fromkeys(self.services, False)
    snapshot = read_snapshot()
    if snapshot is None:
      self.valid = dict.fromkeys(self.services, False)
      self.alive = dict.fromkeys(self.services, False)
      return
    received, value = snapshot
    if received == self.last_received:
      return
    self.last_received = received
    shift = received - value['sent']
    for name in self.services:
      if name not in SERVICES:
        continue
      self.valid[name] = bool(value['valid'].get(name, False))
      self.alive[name] = bool(value['alive'].get(name, False))
      self.recv_time[name] = value['received'].get(name, 0.) + shift
      mono = value['mono'].get(name, 0)
      self.logMonoTime[name] = int(mono + shift * 1e9)
      if mono == self.generations.get(name) or name not in value['events']:
        continue
      raw = base64.b64decode(value['events'][name])
      with self.log.Event.from_bytes(raw) as reader:
        if reader.which() != name:
          raise ValueError('display service identity mismatch')
        self.data[name] = reader.as_builder()
      self.generations[name] = mono
      self.updated[name] = True


def main():
  from hud_quality import NAMES, PARAM, selection
  from hud_quality import CpuJpegError, codec_options
  import hud_quality
  override = None
  if '--jetson-quality-profile' in sys.argv:
    index = sys.argv.index('--jetson-quality-profile')
    if index + 1 >= len(sys.argv) or sys.argv[index + 1] not in NAMES:
      raise SystemExit('Unknown Jetson HUD quality profile')
    override = NAMES.index(sys.argv[index + 1])
    del sys.argv[index:index + 2]
  if '--help' not in sys.argv:
    from boot_update import wait_for_runtime
    wait_for_runtime()
  if '--help' not in sys.argv:
    settings = DisplayParams()
    while not (settings.get_int('ClusterHud') == 1 and
               (settings.get_bool('IsOnroad') or settings.get_int('ClusterHudDebug') >= 1)):
      time.sleep(.2)
  from openpilot.cereal import log
  params_module = types.ModuleType('openpilot.common.params')
  params_module.Params = DisplayParams
  sys.modules[params_module.__name__] = params_module
  messaging = types.ModuleType('openpilot.cereal.messaging')
  messaging.SubMaster = RemoteSubMaster
  from hud_navi import RemoteMediaSocket, MediaPump
  def sub_sock(service, **kwargs):
    if service != 'carrotNaviMedia':
      raise ValueError(f'unsupported display subscription: {service}')
    return RemoteMediaSocket()
  messaging.sub_sock = sub_sock
  messaging.drain_sock = lambda sock: sock.drain()
  def from_bytes(data, schema=log.Event):
    with schema.from_bytes(data) as reader:
      return reader.as_builder()
  messaging.log_from_bytes = from_bytes
  sys.modules[messaging.__name__] = messaging
  from hud_signal import install as install_signal_display
  remote_signals = install_signal_display(read_snapshot)
  import cluster_live_camera
  from hud_camera import RemoteRoadCamera
  cluster_live_camera.LiveRoadCamera = RemoteRoadCamera
  import cluster_system_monitor
  import hud_runtime_stats  # Preserve the actual Jetson sampler for PV5 display.
  from hud_stats import VehicleSystemStats, VehicleCpuOverlay, VehicleNetworkAddress
  cluster_system_monitor.SystemStatsSampler = VehicleSystemStats
  cluster_system_monitor.ClusterProcessCoreUsageSampler = VehicleCpuOverlay
  cluster_system_monitor.NetworkAddressProvider = VehicleNetworkAddress
  import cluster_navi_source
  display_metrics = {}
  from openpilot.selfdrive.navigation import preview as nav_preview
  from hud_nav_map import RemoteNavPreview
  remote_nav_preview = RemoteNavPreview(read_snapshot, nav_preview.PREVIEW_PATH)
  nav_preview.preview_fresh = remote_nav_preview.fresh
  BaseNaviSource = cluster_navi_source.NaviIpcMediaSource
  class RemoteNaviSource:
    def __init__(self, *args, **kwargs):
      self.source = BaseNaviSource(*args, **kwargs)
      self.pump = MediaPump(self.source)

    def update(self, navi_live):
      dashboard = self.pump.update(navi_live)
      frame = next((f for f in dashboard.media if f.key == 'render:map_main' and f.present), None)
      display_metrics['map'] = {'sequence': frame.sequence if frame else None,
                                'width': frame.width if frame else None,
                                'height': frame.height if frame else None,
                                'age_ms': dashboard.map_frame_age_ms,
                                'stalled': dashboard.map_stream_stalled,
                                'decode_drops': self.source._h264_worker.dropped_requests,
                                'received': dashboard.received_count}
      return dashboard

    def close(self):
      self.pump.close()
  cluster_navi_source.NaviIpcMediaSource = RemoteNaviSource
  vehicle_stats = VehicleSystemStats()
  import main as cluster
  from hud_health import HealthRenderer
  cluster.ClusterUiRenderer = HealthRenderer
  if '--help' not in sys.argv:
    scan = cluster.find_supported_usb_product
    wait_for_usb_display(scan, cluster.product_id_for_hud_mode(1))
    # The common PC renderer falls back to an invisible window if the USB
    # panel is absent. A headless Jetson must wait/retry, including the race
    # where it disappears between the preflight scan and opening the device.
    cluster.find_supported_usb_product = lambda expected: require_usb_display(scan, expected)
  class Display(cluster.TuringUsbDisplay):
    next_status = 0.

    def encode_jpeg(self, *args, **kwargs):
      try:
        if self.jpeg_encoder == 'turbojpeg':
          from hud_cpu_jpeg import Encoder
          if getattr(self, '_cpu_jpeg', None) is None:
            self._cpu_jpeg = Encoder()
          return self._cpu_jpeg.encode(*args, quality=self.jpeg_quality, **kwargs)
        return super().encode_jpeg(*args, **kwargs)
      except (ImportError, OSError, RuntimeError, AttributeError, ValueError, TypeError) as exc:
        raise CpuJpegError('CPU JPEG encoder unavailable or failed') from exc

    def close(self):
      try:
        super().close()
      finally:
        self._cpu_jpeg = None

    def send_jpeg(self, *args, **kwargs):
      result = super().send_jpeg(*args, **kwargs)
      self.publish_status()
      return result

    def send_h264_chunk(self, *args, **kwargs):
      result = super().send_h264_chunk(*args, **kwargs)
      self.publish_status()
      return result

    def publish_status(self):
      now = time.monotonic()
      if now >= self.next_status:
        path = Path('/dev/shm/carrot-jetlink-hud-status.json')
        tmp = path.with_suffix('.tmp')
        stats = vehicle_stats.sample()
        remote_nav_preview.fresh()
        display_metrics['nav_model_map'] = remote_nav_preview.status
        signal_color, signal_held, signal_online = remote_signals.status()
        display_metrics['signal_display'] = {'enabled': remote_signals.enabled, 'color': signal_color,
                                             'held': signal_held, 'online': signal_online}
        tmp.write_text(json.dumps({'updated': now, **display_metrics,
                                  'quality': self_quality(),
                                  'vehicle_network_address': VehicleNetworkAddress().address(),
                                  'vehicle_cpu': stats.cpu_core_percents,
                                  'vehicle_memory_percent': stats.memory_used_percent}))
        os.replace(tmp, path)
        self.next_status = now + 1
  cluster.TuringUsbDisplay = Display
  # Jetson has no video encoder block. Keep inference CPU/GPU headroom and
  # use the existing software encoder at the normal 10 FPS cluster rate.
  # Bound conversion/encoding concurrency; an explicit trailing CLI value of
  # zero restores FFmpeg's automatic thread pools for comparisons.
  settings, extra_args = DisplayParams(), sys.argv[1:]
  quality_vehicle = RemoteSubMaster(['carState'])
  def profile_allowed(target):
    # Baseline recovery is always available. New experiments wait for a fresh
    # stationary Park record onroad; offroad/explicit bench overrides can apply.
    if target == 0 or override is not None or not settings.get_bool('IsOnroad'):
      return True
    from hud_health import vehicle_motion_fresh
    snapshot = read_snapshot()
    if not vehicle_motion_fresh(snapshot, time.monotonic()):
      return False
    quality_vehicle.update(0)
    car = quality_vehicle['carState']
    return str(car.gearShifter) == 'park' and car.vEgo == 0.
  class QualityWatch:
    def read(self):
      target = selection(settings.get_int(PARAM))
      return int(override is None and target != requested and profile_allowed(target))
  # Reuse the renderer's clean, main-loop exit instead of throwing from a USB
  # sender thread. This adapter is private to the host and does not change the
  # comma's ClusterHudEncoder behavior. A zero sentinel means unchanged.
  cluster.ClusterHudEncoderParamReader = QualityWatch
  def self_quality():
    from hud_runtime_stats import quality_status
    return quality_status()
  from display_owner import panel_owner
  base = [__file__, '--input', 'live', '--output', 'usb', '--fps', '10', '--cluster-hud-mode', '1',
          '--cluster-hud-encoder', '0']
  requested = 0
  while True:
    target = selection(settings.get_int(PARAM)) if override is None else override
    if profile_allowed(target):
      requested = target
    effective, error = requested, ''
    while True:
      hud_quality.RUN_PROFILE = (requested, effective, error)
      hud_runtime_stats.QUALITY_STATUS = {}
      sys.argv = [*base, *codec_options(NAMES[effective]), *extra_args]
      try:
        if '--help' in sys.argv:
          cluster.main()
        else:
          with panel_owner(hud=True):
            cluster.main(exit_on_error=False)
        if QualityWatch().read():
          break  # Recreate only the display; finally paths close all buffers.
        return
      except CpuJpegError:
        if not effective:
          raise
        effective, error = 0, 'CPU JPEG unavailable; using baseline H.264'
        print(error, flush=True)


if __name__ == '__main__':
  main()
