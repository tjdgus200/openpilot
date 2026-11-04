import time

import numpy as np
import pyray as rl

from openpilot.cereal import log
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import SBIGMODEL_INPUT_SIZE, WIDE_MODEL_FOV_CHOICES, get_warp_matrix, wider_model_warp
from openpilot.system.ui.lib.text_draw import draw_text_ui_style

_WIDTH, _HEIGHT = SBIGMODEL_INPUT_SIZE
_CORNERS = np.array([[0, _WIDTH - 1, _WIDTH - 1, 0], [0, 0, _HEIGHT - 1, _HEIGHT - 1], [1, 1, 1, 1]])
_VIEW_SECONDS = 2.0


def source_polygon(warp):
  points = warp @ _CORNERS
  if not np.isfinite(points).all() or np.any(points[2] <= 0):
    return None
  return (points[:2] / points[2]).T


class StoppedFovPreview:
  def __init__(self):
    self.active = False
    self.wide = False
    self._stopped_since = None
    self._next_poll = 0.0
    self._selection = 0
    self._yaw_trim = 0.0
    self._pitch_preview = 0
    self._wide_enabled = True
    self._key = None
    self._polygon = None
    self._candidate = None

  def update(self, state, road_available, wide_available):
    self.active = False
    cp = state.CP
    if not state.started or cp is None or cp.notCar or cp.carFingerprint != "KIA_PV5":
      self._stopped_since = None
      return

    sm = state.sm
    now = time.monotonic()
    car = sm["carState"]
    if not (sm.valid["carState"] and sm.alive["carState"] and 0 <= now - sm.recv_time["carState"] <= 0.3
            and car.canValid and not car.canTimeout and car.vEgoRaw == 0 and abs(car.vEgo) <= 0.01):
      self._stopped_since = None
      return
    if not (sm.valid["liveCalibration"] and sm.alive["liveCalibration"]
            and sm.seen["roadCameraState"] and sm.seen["deviceState"]):
      self._stopped_since = None
      return
    calib = sm["liveCalibration"]
    if calib.calStatus != log.LiveCalibrationData.Status.calibrated or len(calib.rpyCalib) != 3:
      self._stopped_since = None
      return
    camera = DEVICE_CAMERAS.get((str(sm["deviceState"].deviceType), str(sm["roadCameraState"].sensor)))
    if camera is None:
      self._stopped_since = None
      return

    if now >= self._next_poll:
      self._next_poll = now + 0.25
      self._selection = state.params.get_int("Pv5WideModelFov")
      self._yaw_trim = state.params.get_float("CameraYawTrimDeg") * 0.01
      self._pitch_preview = max(-30, min(30, state.params.get_int("Pv5FovPreviewPitch")))
      self._wide_enabled = bool(state.params.get("UseWideCamera", return_default=True))
    wide_available = wide_available and self._wide_enabled and bool(camera.ecam.width)
    if not (road_available or wide_available):
      self._stopped_since = None
      return
    if self._stopped_since is None:
      self._stopped_since = now
    self.wide = wide_available and (not road_available or int((now - self._stopped_since) / _VIEW_SECONDS) % 2 == 0)
    lens = camera.ecam if self.wide else camera.fcam
    fov = (WIDE_MODEL_FOV_CHOICES[self._selection - 1]
           if self.wide and 1 <= self._selection <= len(WIDE_MODEL_FOV_CHOICES) else None)
    key = lens, self.wide, sm.recv_frame["liveCalibration"], fov, self._yaw_trim, self._pitch_preview
    if key != self._key:
      self._key = key
      rpy = np.array(calib.rpyCalib, dtype=np.float32)
      rpy[2] -= np.radians(self._yaw_trim)
      self._polygon = self._project(rpy, lens, fov)
      self._candidate = None
      if self._pitch_preview:
        # Positive angles move the candidate region down in the source image.
        rpy[1] -= np.radians(self._pitch_preview * 0.1)
        self._candidate = self._project(rpy, lens, fov)
    self.active = self._polygon is not None
    self._source_size = lens.size
    self._label = (f"MODEL WIDE {fov}°" if fov else "MODEL WIDE ~59°") if self.wide else "MODEL ROAD ~31°"

  def _project(self, rpy, lens, fov):
    warp = get_warp_matrix(rpy, lens.intrinsics, self.wide)
    return source_polygon(wider_model_warp(warp, fov) if fov else warp.astype(np.float32))

  def draw(self, frame, dst):
    if not self.active or (frame.width, frame.height) != self._source_size:
      return
    center = dst.x + dst.width * 0.5
    y = dst.y + dst.height * 0.12
    for polygon, label, color in (
      (self._polygon, self._label, rl.Color(65, 231, 250, 255)),
      (self._candidate, f"PREVIEW {self._pitch_preview * 0.1:+.1f}°", rl.Color(255, 189, 75, 255)),
    ):
      if polygon is None:
        continue
      points = [rl.Vector2(dst.x + float(x) * dst.width / frame.width,
                          dst.y + float(y) * dst.height / frame.height) for x, y in polygon]
      for i in range(len(points)):
        rl.draw_line_ex(points[i - 1], points[i], 4.0, color)
      draw_text_ui_style(label, center, y, 28, color, align="center_top", y_offset=0.0)
      y += 34
