"""Opt-in capture metadata. No camera pixels are sent via this channel."""
from collections import deque
from functools import partial
from itertools import islice
import json
import socket
import time

MARKER_ADDRESS = '\0carrot.pv5.radar-labels.wide.v2'
LENS_BYTES = 6 * 128 * 256
OFFSETS = (0, 12289, 24593, 36871, 49157, 61441, 73751, 86017,
           98317, 110593, 122897, 135169, 147457, 159757, 172057, 184321)
MAX_RADAR_AGE_NS = 100_000_000
MAX_MARKER_AGE_NS = 250_000_000
MAX_CALIBRATION_AGE_NS = 1_000_000_000
DRAIN_LIMIT = 8


class CaptureMarker:
  def __init__(self):
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    self.sock.setblocking(False)
    self.geometry = None
    self.previous_calibration_ns = None
    self.image = None

  def calibrate(self, calibration, stamp, intrinsics, warp, valid):
    import numpy as np
    previous_geometry, self.geometry = self.geometry, None
    previous_calibration_ns, self.previous_calibration_ns = self.previous_calibration_ns, None
    if not valid:
      return
    try:
      height = float(calibration.height[0])
      if 0.3 < height < 4 and len(calibration.wideFromDeviceEuler) == len(calibration.rpyCalib) == 3:
        from openpilot.common.transformations.camera import view_frame_from_device_frame
        from openpilot.common.transformations.orientation import rot_from_euler
        # liveTracks is forward / left / up. The camera calibration uses forward / right / down.
        wide_from_device = rot_from_euler(calibration.wideFromDeviceEuler)
        view = view_frame_from_device_frame @ wide_from_device
        axes = rot_from_euler(calibration.rpyCalib) @ np.diag([1., -1., -1.])
        ground = intrinsics @ view @ np.column_stack((axes, [0., 0., height]))
        matrix = np.linalg.solve(warp, ground)
        if np.isfinite(matrix).all():
          stamp, projection = int(stamp), matrix.tolist()
          self.geometry = stamp, projection
          if previous_geometry is not None and previous_geometry[1] == projection:
            previous_stamp = previous_geometry[0]
            if 0 < stamp - previous_stamp <= MAX_CALIBRATION_AGE_NS and previous_stamp > 0:
              self.previous_calibration_ns = previous_stamp
            elif stamp == previous_stamp:
              self.previous_calibration_ns = previous_calibration_ns
    except (AttributeError, TypeError, IndexError, ValueError, ArithmeticError, np.linalg.LinAlgError):
      pass

  def identify(self, frame, images):
    self.image = None
    if self.geometry is not None:
      try:
        view = memoryview(images).cast('B')
        if len(view) == 2 * LENS_BYTES:
          self.image = frame, bytes(view[LENS_BYTES + i] for i in OFFSETS).hex()
      except (ValueError, TypeError, IndexError, BufferError):
        pass

  def publish(self, frame, capture_ns):
    sample, self.image = self.image, None
    if not sample or sample[0] != frame or self.geometry is None:
      return
    calibration_ns, projection = self.geometry
    previous = self.previous_calibration_ns
    # The camera frame may already be captured when modeld consumes a new calibration.
    # An earlier observation is valid here only if it represents the same projection.
    if (previous is not None and previous <= capture_ns < calibration_ns
        and capture_ns - previous <= MAX_CALIBRATION_AGE_NS):
      calibration_ns = previous
    record = dict(frame=frame, capture_ns=capture_ns, calibration_ns=calibration_ns,
                  lens='wide', projection=projection, fingerprint=sample[1])
    try:
      self.sock.sendto(json.dumps(record, separators=(',', ':')).encode(), MARKER_ADDRESS)
    except OSError:
      pass


class RadarContext:
  def __init__(self, receive_radar=None):
    if receive_radar is None:
      from openpilot.cereal import messaging
      receive_radar = partial(messaging.recv_one_or_none, messaging.sub_sock('liveTracks', conflate=False))
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
      self.sock.setblocking(False)
      self.sock.bind(MARKER_ADDRESS)
    except OSError:
      self.sock.close()
      raise
    self.receive_radar = receive_radar
    self.radar_epoch = time.monotonic_ns()
    self.last_radar_ns = 0
    self.radar = deque(maxlen=DRAIN_LIMIT)
    self.recent = deque(maxlen=6)

  def close(self):
    self.sock.close()
    # The native SubSocket releases its queue reader when the reference drops.
    self.receive_radar = None

  def _forget_radar(self):
    self.radar.clear()
    self.recent.clear()
    self.radar_epoch = max(self.radar_epoch + 1, time.monotonic_ns())

  def _sample_radar(self):
    # The HUD's conflating SubMaster only sees the newest sample of its period.
    # Drain the dedicated nonblocking queue to retain samples before each capture.
    events = list(islice(iter(self.receive_radar, None), DRAIN_LIMIT + 1))
    if len(events) > DRAIN_LIMIT:
      # A backlog may hide a newer invalid event. Drop this generation.
      self._forget_radar()
      return
    now = time.monotonic_ns()
    for event in events:
      stamp = event.logMonoTime
      if event.which() != 'liveTracks' or stamp <= self.last_radar_ns or not 0 <= now - stamp <= MAX_MARKER_AGE_NS:
        self._forget_radar()
        continue
      if stamp - self.last_radar_ns > MAX_MARKER_AGE_NS:
        self._forget_radar()
      self.last_radar_ns = stamp
      radar = event.liveTracks
      if (not event.valid or radar.radarTrackFlipped or len(radar.points) > 128
          or any(radar.errors.to_dict().values())):
        self._forget_radar()
        continue
      points = [[p.trackId, p.dRel, p.yRel, p.vRel] for p in radar.points
                if p.measured and str(p.radarSource) == 'frontRadar']
      self.radar.append((stamp, points))
    if now - self.last_radar_ns > MAX_MARKER_AGE_NS:
      self._forget_radar()

  def sample(self):
    self._sample_radar()
    now = time.monotonic_ns()
    for _ in range(DRAIN_LIMIT):
      try:
        marker = json.loads(self.sock.recv(2048))
      except BlockingIOError:
        break
      if not isinstance(marker, dict) or marker.get('lens') != 'wide':
        continue
      capture = marker['capture_ns']
      if 0 <= now - capture <= MAX_MARKER_AGE_NS:
        for stamp, points in reversed(self.radar):
          if 0 <= capture - stamp <= MAX_RADAR_AGE_NS:
            self.recent.append(dict(marker, radar_ns=stamp, radar_epoch=self.radar_epoch, points=points))
            break
    return [m for m in self.recent if 0 <= now - m['capture_ns'] <= MAX_MARKER_AGE_NS] or None
