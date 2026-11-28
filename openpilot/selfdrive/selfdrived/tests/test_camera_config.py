from types import SimpleNamespace

import pytest

from openpilot.common import pv5_parked_dashcam
from openpilot.selfdrive.selfdrived.camera_config import get_camera_packets


@pytest.mark.parametrize('wide,expected', [
  (True, ['roadCameraState', 'wideRoadCameraState']),
  (False, ['roadCameraState']),
])
def test_road_camera_health_is_independent_of_driver_camera_fallback(wide, expected):
  assert get_camera_packets(wide) == expected


@pytest.mark.parametrize('wide,replay,overrides,required', [
  (True, False, {}, True),
  (False, False, {}, False),
  (True, True, {}, False),
  (True, False, {'brand': 'mock'}, False),
  (True, False, {'carFingerprint': 'OTHER'}, False),
  (True, False, {'notCar': True}, False),
  (True, False, {'passive': True}, False),
])
def test_vehicle_scope_of_required_camera_pair(monkeypatch, wide, replay, overrides, required):
  cp = SimpleNamespace(**({'brand': 'hyundai', 'carFingerprint': 'KIA_PV5', 'notCar': False, 'passive': False} | overrides))
  monkeypatch.setattr(pv5_parked_dashcam, 'supported', lambda replay: not replay)
  assert pv5_parked_dashcam.requires_dual_road_cameras(cp, wide, replay) is required
