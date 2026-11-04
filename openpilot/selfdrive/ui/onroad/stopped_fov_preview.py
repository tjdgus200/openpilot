import time

import numpy as np
import pyray as rl

from openpilot.cereal import log
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import SBIGMODEL_INPUT_SIZE, WIDE_MODEL_FOV_CHOICES, get_warp_matrix, wider_model_warp
from openpilot.system.ui.lib.text_draw import draw_text_ui_style

_WIDTH, _HEIGHT = SBIGMODEL_INPUT_SIZE
_CORNERS = np.array([[0, _WIDTH - 1, _WIDTH - 1, 0], [0, 0, _HEIGHT - 1, _HEIGHT - 1], [1, 1, 1, 1]])


def source_polygon(warp):
  points = warp @ _CORNERS
  if not np.isfinite(points).all() or np.any(points[2] <= 0):
    return None
  return (points[:2] / points[2]).T


class StoppedFovPreview:
  def __init__(self):
    self.active = False
    self._next_poll = 0.0
    self._selection = 0
    self._yaw_trim = 0.0
    self._key = None
    self._polygon = None

  def update(self, state, wide_available):
    self.active = False
    cp = state.CP
    if not state.started or cp is None or cp.notCar or cp.carFingerprint != "KIA_PV5" or not wide_available:
      return

    sm = state.sm
    now = time.monotonic()
    if not (sm.valid["carState"] and sm.alive["carState"] and 0 <= now - sm.recv_time["carState"] <= 0.3
            and abs(sm["carState"].vEgo) <= 0.01):
      return
    if not (sm.valid["liveCalibration"] and sm.seen["roadCameraState"] and sm.seen["deviceState"]):
      return
    calib = sm["liveCalibration"]
    if calib.calStatus != log.LiveCalibrationData.Status.calibrated or len(calib.rpyCalib) != 3:
      return
    camera = DEVICE_CAMERAS.get((str(sm["deviceState"].deviceType), str(sm["roadCameraState"].sensor)))
    if camera is None or not camera.ecam.width:
      return

    if now >= self._next_poll:
      self._next_poll = now + 0.25
      self._selection = state.params.get_int("Pv5WideModelFov")
      self._yaw_trim = state.params.get_float("CameraYawTrimDeg") * 0.01
    fov = WIDE_MODEL_FOV_CHOICES[self._selection - 1] if 1 <= self._selection <= len(WIDE_MODEL_FOV_CHOICES) else None
    key = camera.ecam, sm.recv_frame["liveCalibration"], fov, self._yaw_trim
    if key != self._key:
      self._key = key
      rpy = np.array(calib.rpyCalib, dtype=np.float32)
      rpy[2] -= np.radians(self._yaw_trim)
      warp = get_warp_matrix(rpy, camera.ecam.intrinsics, True)
      self._polygon = source_polygon(wider_model_warp(warp, fov) if fov else warp.astype(np.float32))
    self.active = self._polygon is not None
    self._source_size = camera.ecam.size
    self._label = f"MODEL FOV {fov}°" if fov else "MODEL FOV ~59°"

  def draw(self, frame, dst):
    if not self.active or (frame.width, frame.height) != self._source_size:
      return
    points = [rl.Vector2(dst.x + float(x) * dst.width / frame.width,
                        dst.y + float(y) * dst.height / frame.height) for x, y in self._polygon]
    color = rl.Color(65, 231, 250, 255)
    for i in range(len(points)):
      rl.draw_line_ex(points[i - 1], points[i], 4.0, color)
    draw_text_ui_style(self._label, dst.x + dst.width * 0.5, dst.y + dst.height * 0.12, 28, color, align="center_top", y_offset=0.0)
