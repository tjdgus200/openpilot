import math

import numpy as np

from openpilot.common.transformations.orientation import rot_from_euler
from openpilot.common.transformations.camera import get_view_frame_from_calib_frame, view_frame_from_device_frame, _ar_ox_fisheye

# segnet
SEGNET_SIZE = (512, 384)

# MED model
MEDMODEL_INPUT_SIZE = (512, 256)
MEDMODEL_YUV_SIZE = (MEDMODEL_INPUT_SIZE[0], MEDMODEL_INPUT_SIZE[1] * 3 // 2)
MEDMODEL_CY = 47.6

medmodel_fl = 910.0
medmodel_intrinsics = np.array([
  [medmodel_fl,  0.0,  0.5 * MEDMODEL_INPUT_SIZE[0]],
  [0.0,  medmodel_fl,                   MEDMODEL_CY],
  [0.0,  0.0,                                   1.0]])


# BIG model
BIGMODEL_INPUT_SIZE = (1024, 512)
BIGMODEL_YUV_SIZE = (BIGMODEL_INPUT_SIZE[0], BIGMODEL_INPUT_SIZE[1] * 3 // 2)

bigmodel_fl = 910.0
bigmodel_intrinsics = np.array([
  [bigmodel_fl,  0.0,  0.5 * BIGMODEL_INPUT_SIZE[0]],
  [0.0,  bigmodel_fl,             256 + MEDMODEL_CY],
  [0.0,  0.0,                                   1.0]])


# SBIG model (big model with the size of small model)
SBIGMODEL_INPUT_SIZE = (512, 256)
SBIGMODEL_YUV_SIZE = (SBIGMODEL_INPUT_SIZE[0], SBIGMODEL_INPUT_SIZE[1] * 3 // 2)

sbigmodel_fl = 455.0
sbigmodel_intrinsics = np.array([
  [sbigmodel_fl,  0.0,  0.5 * SBIGMODEL_INPUT_SIZE[0]],
  [0.0,  sbigmodel_fl,      0.5 * (256 + MEDMODEL_CY)],
  [0.0,  0.0,                                     1.0]])

WIDE_MODEL_FOV_CHOICES = (65, 70, 75, 80)
WIDE_MODEL_DOWN_MAX = 30  # Stored tenths of a degree; experimental, not a calibration correction.


def wide_model_pitch(rpy: np.ndarray, down: int) -> np.ndarray:
  """Tilt only the virtual wide model view down, leaving live calibration untouched."""
  if not isinstance(down, (int, np.integer)) or isinstance(down, bool) or not 0 <= down <= WIDE_MODEL_DOWN_MAX:
    raise ValueError(f"Unsupported wide model down offset: {down}")
  result = rpy.copy()
  if down:
    result[1] -= math.radians(down * 0.1)
  return result


def wider_model_warp(stock_warp: np.ndarray, fov: int) -> np.ndarray:
  """Keep the stock optical center while expanding the wide model view."""
  if fov not in WIDE_MODEL_FOV_CHOICES:
    raise ValueError(f"Unsupported experimental FOV: {fov}")
  intrinsics = sbigmodel_intrinsics.copy()
  focal = SBIGMODEL_INPUT_SIZE[0] / (2 * math.tan(math.radians(fov / 2)))
  intrinsics[0, 0] = intrinsics[1, 1] = focal
  return (stock_warp @ sbigmodel_intrinsics @ np.linalg.inv(intrinsics)).astype(np.float32)


DM_INPUT_SIZE = (1440, 960)
dmonitoringmodel_fl = _ar_ox_fisheye.focal_length
dmonitoringmodel_intrinsics = np.array([
  [dmonitoringmodel_fl,  0.0, DM_INPUT_SIZE[0]/2],
  [0.0, dmonitoringmodel_fl, DM_INPUT_SIZE[1]/2 - (_ar_ox_fisheye.height - DM_INPUT_SIZE[1])/2],
  [0.0,  0.0, 1.0]])

bigmodel_frame_from_calib_frame = np.dot(bigmodel_intrinsics,
  get_view_frame_from_calib_frame(0, 0, 0, 0))


sbigmodel_frame_from_calib_frame = np.dot(sbigmodel_intrinsics,
  get_view_frame_from_calib_frame(0, 0, 0, 0))

medmodel_frame_from_calib_frame = np.dot(medmodel_intrinsics,
  get_view_frame_from_calib_frame(0, 0, 0, 0))

medmodel_frame_from_bigmodel_frame = np.dot(medmodel_intrinsics, np.linalg.inv(bigmodel_intrinsics))

calib_from_medmodel = np.linalg.inv(medmodel_frame_from_calib_frame[:, :3])
calib_from_sbigmodel = np.linalg.inv(sbigmodel_frame_from_calib_frame[:, :3])

# This function is verified to give similar results to xx.uncommon.utils.transform_img
def get_warp_matrix(device_from_calib_euler: np.ndarray, intrinsics: np.ndarray, bigmodel_frame: bool = False) -> np.ndarray:
  calib_from_model = calib_from_sbigmodel if bigmodel_frame else calib_from_medmodel
  device_from_calib = rot_from_euler(device_from_calib_euler)
  camera_from_calib = intrinsics @ view_frame_from_device_frame @ device_from_calib
  warp_matrix: np.ndarray = camera_from_calib @ calib_from_model
  return warp_matrix
