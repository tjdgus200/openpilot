"""Opt-in capture metadata. No camera pixels are sent via this channel."""
from collections import deque
import json
import socket
import time

MARKER_ADDRESS = '\0carrot.pv5.radar-labels.wide.v2'
LENS_BYTES = 6 * 128 * 256
OFFSETS = (0, 12289, 24593, 36871, 49157, 61441, 73751, 86017,
           98317, 110593, 122897, 135169, 147457, 159757, 172057, 184321)


class CaptureMarker:
  def __init__(self):
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    self.sock.setblocking(False)
    self.geometry = None
    self.image = None

  def calibrate(self, calibration, stamp, intrinsics, warp, valid):
    import numpy as np
    self.geometry = None
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
          self.geometry = int(stamp), matrix.tolist()
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
    record = dict(frame=frame, capture_ns=capture_ns, calibration_ns=calibration_ns,
                  lens='wide', projection=projection, fingerprint=sample[1])
    try:
      self.sock.sendto(json.dumps(record, separators=(',', ':')).encode(), MARKER_ADDRESS)
    except OSError:
      pass


class RadarContext:
  def __init__(self):
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    self.sock.setblocking(False)
    self.sock.bind(MARKER_ADDRESS)
    self.radar = deque(maxlen=4)
    self.recent = deque(maxlen=6)

  def sample(self, sm):
    if sm.updated['liveTracks']:
      radar = sm['liveTracks']
      points = []
      if (sm.valid['liveTracks'] and not radar.radarTrackFlipped and len(radar.points) <= 128
          and not any(radar.errors.to_dict().values())):
        points = [[p.trackId, p.dRel, p.yRel, p.vRel] for p in radar.points
                  if p.measured and str(p.radarSource) == 'frontRadar']
      self.radar.append((sm.logMonoTime['liveTracks'], points))
    for _ in range(8):
      try:
        marker = json.loads(self.sock.recv(2048))
      except BlockingIOError:
        break
      if not isinstance(marker, dict) or marker.get('lens') != 'wide':
        continue
      capture = marker['capture_ns']
      if 0 <= time.monotonic_ns() - capture <= 250_000_000:
        for stamp, points in reversed(self.radar):
          if 0 <= capture - stamp <= 100_000_000:
            self.recent.append(dict(marker, radar_ns=stamp, points=points))
            break
    now = time.monotonic_ns()
    return [m for m in self.recent if 0 <= now - m['capture_ns'] <= 250_000_000] or None
