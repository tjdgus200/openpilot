import math
from types import SimpleNamespace as NS

import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.exposure import FOV_STATUS_LABELS, exposure_label, exposure_rectangle, fov_status_label


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


@pytest.mark.parametrize('code', range(1, 22))
def test_status_codes_have_preview_labels_and_unknown_packets_remain_compatible(code):
  label = fov_status_label(NS(pv5ExposureFovStatus=code))
  assert label and code in FOV_STATUS_LABELS
  assert '미지원' not in label or code in (9, 20)
  assert not fov_status_label(NS()) and not fov_status_label(NS(pv5ExposureFovStatus=0))
  assert fov_status_label(NS(pv5ExposureFovStatus=255)) == '측광 상태 정보 미지원'


@pytest.mark.parametrize('elapsed,text', [(0, '0.0'), (50, '0.0'), (150, '0.1'), (1200, '1.2'), (1950, '1.9'), (2000, '1.9')])
def test_warmup_label_does_not_claim_completion_before_the_active_status(elapsed, text):
  assert fov_status_label(NS(pv5ExposureFovStatus=3, pv5ExposureFovWarmupMs=elapsed)) == f'정상 확인 중 {text}/2.0초'


@pytest.mark.parametrize('elapsed', [None, 'bad', math.nan, math.inf, -1, 2001])
def test_invalid_diagnostic_progress_does_not_crash_or_show_a_false_duration(elapsed):
  assert fov_status_label(NS(pv5ExposureFovStatus=3, pv5ExposureFovWarmupMs=elapsed)) == '정상 확인 중'


@pytest.mark.parametrize('service', ['roadCameraState', 'wideRoadCameraState'])
def test_diagnostic_fields_round_trip_with_the_real_camera_schema(service):
  from openpilot.cereal import log
  event = log.Event.new_message()
  packet = event.init(service)
  packet.pv5ExposureFovStatus = 3
  packet.pv5ExposureFovWarmupMs = 1250
  with log.Event.from_bytes(event.to_bytes()) as decoded:
    assert fov_status_label(getattr(decoded, service)) == '정상 확인 중 1.2/2.0초'
