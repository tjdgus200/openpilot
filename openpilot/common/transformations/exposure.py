"""Read-only reconstruction of camerad's C3-family automatic-exposure ROI.

This is an overlay, never an exposure controller or a model input transform.
Unknown geometries are omitted rather than displaying an approximate rectangle.
"""
import math


def exposure_rectangle(device, sensor, lens, wide):
  if (str(device) not in ('tici', 'tizi') or str(sensor) not in ('unknown', 'ar0231', 'ox03c10')
      or lens.size != (1928, 1208) or not math.isfinite(lens.focal_length)):
    return None
  # camera_qcom2.cc::CameraState::set_exposure_rect; preserve its integer casts.
  y, width, height, focal = (400, 1734, 524, 567.) if wide else (160, 1734, 986, 2648.)
  scale = lens.focal_length / focal
  image_width, image_height = lens.size
  return (
    max(0, image_width // 2 - int(scale * width / 2)),
    max(0, image_height // 2 - int(scale * (1208 / 2 - y))),
    min(int(scale * width), image_width // 2 + int(scale * width / 2)),
    min(int(scale * height), image_height // 2 + int(scale * (1208 / 2 - y))),
  )


def exposure_label(camera):
  """Use only finite published exposure telemetry; no pixel readback."""
  grey, target, gain = camera.measuredGreyFraction, camera.targetGreyFraction, camera.gain
  if not all(math.isfinite(v) for v in (grey, target, gain)) or not (0 <= grey <= 1 and 0 < target <= 1 and gain > 0):
    return 'AE --'
  return f'AE grey {grey:.2f}/{target:.2f} | gain {gain:.1f}'
