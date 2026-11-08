"""Read-only reconstruction of camerad's C3-family automatic-exposure ROI.

This is an overlay, never an exposure controller or a model input transform.
Unknown geometries are omitted rather than displaying an approximate rectangle.
"""
import math


# Stable diagnostic codes from system/camerad/cameras/model_fov_meter.h.
# Labels are preview-only and never feed exposure or model/control decisions.
FOV_STATUS_LABELS = {
  1: '선택 안 함 | 기존 측광', 2: 'FOV 측광 중', 3: '정상 확인 중',
  4: '보정/장치 정보 대기/무효', 5: '보정 정보 만료 | 기존 측광', 6: '장치 정보 만료 | 기존 측광',
  7: '보정값 이상 | 기존 측광', 8: '보정 미완료 | 기존 측광', 9: '미지원/비활성 구성 | 기존 측광',
  10: '수동 노출 디버그 사용 중', 11: 'yaw 변경 | 재부팅 필요', 12: '영역 잘림 | 기존 측광',
  13: '영상 버퍼 이상 | 기존 측광', 14: '측광 밝기 무효 | 기존 측광',
  15: '반복 오류 잠금 | 해당 카메라 제외 후 다시 선택', 16: '시계 이상 잠금 | 해당 카메라 제외 후 다시 선택',
  17: '측광 정보 수집기 준비/사용 불가', 18: '측광 정보 수집 오류 | 기존 측광',
  19: '정보 시각 이상 | 기존 측광', 20: '차량 정보 대기/미지원', 21: '보정/장치 정보 만료 | 기존 측광',
}


def fov_status_label(camera):
  code = getattr(camera, 'pv5ExposureFovStatus', 0)
  if not code:  # Older camerad packets have no reason; retain their legacy UI.
    return ''
  if code == 3:
    elapsed = getattr(camera, 'pv5ExposureFovWarmupMs', 0)
    if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and 0 <= elapsed <= 2000:
      # Never round 1950ms up to a completed 2.0s interval while still warming.
      return f'정상 확인 중 {min(int(elapsed) // 100, 19) / 10:.1f}/2.0초'
  return FOV_STATUS_LABELS.get(code, '측광 상태 정보 미지원')


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
