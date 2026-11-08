import math
from types import SimpleNamespace as NS

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.exposure import exposure_label, exposure_rectangle


def test_known_c3x_rectangles_match_camerad_and_unknown_is_not_guessed():
  camera = DEVICE_CAMERAS['tizi', 'ox03c10']
  assert exposure_rectangle('tizi', 'ox03c10', camera.ecam, True) == (97, 400, 1734, 524)
  assert exposure_rectangle('tizi', 'ox03c10', camera.fcam, False) == (97, 160, 1734, 986)
  assert exposure_rectangle('mici', 'os04c10', DEVICE_CAMERAS['mici', 'os04c10'].ecam, True) is None
  assert exposure_rectangle('tizi', 'unknown_sensor', camera.ecam, True) is None


def test_missing_exposure_is_not_presented_as_a_valid_measurement():
  assert exposure_label(NS(measuredGreyFraction=.05, targetGreyFraction=.1, gain=2.)) == 'AE grey 0.05/0.10 | gain 2.0'
  assert exposure_label(NS(measuredGreyFraction=math.nan, targetGreyFraction=.1, gain=2.)) == 'AE --'
  assert exposure_label(NS(measuredGreyFraction=.05, targetGreyFraction=0., gain=2.)) == 'AE --'
