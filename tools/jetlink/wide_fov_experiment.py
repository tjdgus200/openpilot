"""Offline PV5 wide-view geometry experiment. Run from the repository root with -m."""

import argparse
import itertools

import numpy as np

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import SBIGMODEL_INPUT_SIZE, WIDE_MODEL_FOV_CHOICES as FOV_CHOICES, get_warp_matrix, wider_model_warp
CAMERAS = {sensor: DEVICE_CAMERAS["mici", sensor].ecam for sensor in ("ox03c10", "os04c10")}


def wide_warp(calibration, intrinsics, fov=None):
  """Map model-image coordinates to the source, keeping the horizon pixel fixed."""
  warp = get_warp_matrix(calibration, intrinsics, bigmodel_frame=True)
  if fov is None:
    return warp.astype(np.float32)
  return wider_model_warp(warp, fov)


def outside_fraction(warp, source_size):
  """Fraction of output luma samples that would require border handling."""
  width, height = SBIGMODEL_INPUT_SIZE
  y, x = np.indices((height, width), dtype=np.float32)
  source_x, source_y, depth = warp @ np.stack((x.ravel(), y.ravel(), np.ones(x.size, np.float32)))
  with np.errstate(divide="ignore", invalid="ignore"):
    source_x, source_y = np.rint(source_x / depth), np.rint(source_y / depth)
  source_width, source_height = source_size
  valid = (depth > 0) & (source_x >= 0) & (source_x < source_width) & (source_y >= 0) & (source_y < source_height)
  return 1 - np.mean(valid)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--fov", type=int, choices=FOV_CHOICES, help="Wide horizontal FOV in degrees. Omit for stock.")
  parser.add_argument("--sweep", action="store_true", help="Compare stock and all four options across a synthetic calibration grid.")
  args = parser.parse_args()
  if args.sweep and args.fov is not None:
    parser.error("--sweep and --fov are mutually exclusive")
  angles = np.radians(list(itertools.product((-3, 0, 3), repeat=3))) if args.sweep else np.zeros((1, 3))
  print("sensor fov  neutral_outside%  worst_outside%")
  for sensor, camera in CAMERAS.items():
    for fov in (None, *FOV_CHOICES) if args.sweep else (args.fov,):
      neutral = outside_fraction(wide_warp(np.zeros(3), camera.intrinsics, fov), camera.size)
      worst = max(outside_fraction(wide_warp(angle, camera.intrinsics, fov), camera.size) for angle in angles)
      print(f"{sensor:7} {fov or 'stock':>5} {100 * neutral:17.3f} {100 * worst:15.3f}")


if __name__ == "__main__":
  main()
