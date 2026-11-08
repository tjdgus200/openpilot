"""Test stopped/opt-in moving previews with real geometry and synthetic state."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import get_warp_matrix, wider_model_warp


@pytest.fixture
def preview():
  source = Path(__file__).parents[1] / "onroad/stopped_fov_preview.py"
  tree = ast.parse(source.read_text(encoding="utf-8"))
  # This test exercises geometry and timing without a display or cereal IPC.
  tree.body = [n for n in tree.body if not (
    isinstance(n, ast.Import) and n.names[0].name == "pyray" or
    isinstance(n, ast.ImportFrom) and n.module in ("openpilot.cereal", "openpilot.system.ui.lib.text_draw"))]
  calibrated = object()
  namespace = {"log": NS(LiveCalibrationData=NS(Status=NS(calibrated=calibrated)))}
  exec(compile(tree, str(source), "exec"), namespace)
  car = NS(canValid=True, canTimeout=False, vEgoRaw=0., vEgo=0.)
  settings = {"Pv5WideModelFov": 4, "CameraYawTrimDeg": 0, "Pv5FovPreviewPitch": 0, "UseWideCamera": True,
              "Pv5CameraFovPreview": False, "Pv5ModelFovExposure": False}
  sm = type("Messages", (dict,), {})(carState=car, deviceState=NS(deviceType="mici"),
                                    roadCameraState=NS(sensor="os04c10"),
                                    liveCalibration=NS(calStatus=calibrated, rpyCalib=[0., 0., 0.]))
  sm.valid = {"carState": True, "liveCalibration": True}
  sm.alive = {"carState": True, "liveCalibration": True}
  sm.seen = {"roadCameraState": True, "deviceState": True}
  sm.recv_time = {"carState": 10.}
  sm.recv_frame = {"liveCalibration": 1}
  state = NS(started=True, CP=NS(notCar=False, carFingerprint="KIA_PV5"), sm=sm,
             params=NS(get_int=settings.__getitem__, get_float=settings.__getitem__, get_bool=settings.__getitem__,
                       get=lambda key, **_: settings[key]))
  viewer = namespace["StoppedFovPreview"]()
  viewer._test_state = state

  def update(now, road=True, wide=True, age=0.):
    namespace["time"] = NS(monotonic=lambda: now)
    sm.recv_time["carState"] = now - age
    viewer.update(state, road, wide)

  return viewer, update, car, settings, namespace["source_polygon"]


def test_stopped_preview_switches_every_two_seconds_and_restarts_after_motion(preview):
  viewer, update, car, _, _ = preview
  for now, wide in ((10., True), (11.99, True), (12., False), (13.99, False), (14., True)):
    update(now)
    assert viewer.active and viewer.wide is wide
  car.vEgoRaw = 0.01
  update(14.1)
  assert not viewer.active
  car.vEgoRaw = 0.
  update(16.1)
  assert viewer.active and viewer.wide
  update(16.2, age=.31)
  assert not viewer.active
  car.canTimeout = True
  update(16.3)
  assert not viewer.active


def test_stopped_preview_matches_each_model_warp_and_keeps_pitch_candidate_separate(preview):
  viewer, update, _, settings, polygon = preview
  camera = DEVICE_CAMERAS[("mici", "os04c10")]
  for now, lens, wide in ((10., camera.ecam, True), (12., camera.fcam, False)):
    update(now)
    expected = get_warp_matrix(np.zeros(3), lens.intrinsics, wide)
    expected = wider_model_warp(expected, 80) if wide else expected.astype(np.float32)
    np.testing.assert_allclose(viewer._polygon, polygon(expected), atol=1e-5)
    assert viewer._candidate is None
  settings["Pv5FovPreviewPitch"] = 10
  for now, lens, wide in ((14., camera.ecam, True), (16., camera.fcam, False)):
    update(now)
    expected = get_warp_matrix(np.zeros(3), lens.intrinsics, wide)
    expected = wider_model_warp(expected, 80) if wide else expected.astype(np.float32)
    np.testing.assert_allclose(viewer._polygon, polygon(expected), atol=1e-5)
    assert np.all(viewer._candidate[:, 1] > viewer._polygon[:, 1])


def test_stopped_preview_keeps_available_camera(preview):
  viewer, update, _, settings, _ = preview
  update(10., wide=False)
  assert viewer.active and not viewer.wide
  update(12., road=False)
  assert viewer.active and viewer.wide
  settings["UseWideCamera"] = False
  update(12.3)
  assert viewer.active and not viewer.wide


def test_c3x_exposure_overlay_uses_fresh_correct_lens_and_never_changes_warp(preview):
  viewer, update, _, _, polygon = preview
  state = viewer._test_state
  sm = state.sm
  sm['deviceState'].deviceType = 'tizi'
  sm['roadCameraState'].sensor = 'ox03c10'
  for name, grey in (('roadCameraState', .2), ('wideRoadCameraState', .05)):
    sm[name] = NS(sensor='ox03c10', measuredGreyFraction=grey, targetGreyFraction=.1, gain=2.)
    sm.valid[name] = sm.alive[name] = True
    sm.recv_time[name] = 10.
  update(10.)
  assert viewer._exposure_rect == (97, 400, 1734, 524)
  assert viewer._exposure_text == 'AE grey 0.05/0.10 | gain 2.0 | STOCK'
  camera = DEVICE_CAMERAS['tizi', 'ox03c10']
  np.testing.assert_allclose(viewer._polygon, polygon(wider_model_warp(get_warp_matrix(np.zeros(3), camera.ecam.intrinsics, True), 80)), atol=1e-5)
  update(12.)
  assert viewer._exposure_rect == (97, 160, 1734, 986)
  assert viewer._exposure_text == 'AE --'  # old telemetry never labels the new lens
  sm.recv_time['roadCameraState'] = 12.6
  update(12.6)
  assert viewer._exposure_text == 'AE grey 0.20/0.10 | gain 2.0 | STOCK'


def test_opt_in_preview_works_while_moving_and_off_restores_stopped_only(preview):
  viewer, update, car, settings, _ = preview
  car.vEgoRaw = car.vEgo = 20.
  update(10.)
  assert not viewer.active
  settings['Pv5CameraFovPreview'] = True
  for now, wide in ((10.3, True), (12.3, False), (14.3, True)):
    update(now)
    assert viewer.active and viewer.wide is wide
  settings['Pv5CameraFovPreview'] = False
  update(14.6)
  assert not viewer.active
  car.vEgoRaw = car.vEgo = 0.
  update(14.9)
  assert viewer.active


def test_active_meter_outline_comes_from_camerad_and_expires_on_lens_switch(preview):
  viewer, update, _, settings, _ = preview
  settings['Pv5ModelFovExposure'] = True
  sm = viewer._test_state.sm
  points = [100., 200., 1000., 200., 1000., 700., 100., 700.]
  sm['wideRoadCameraState'] = NS(measuredGreyFraction=.2, targetGreyFraction=.1, gain=2.,
                                pv5ExposureFovActive=True, pv5ExposureFovPolygon=points,
                                pv5ExposureBlackFraction=.1, pv5ExposureWhiteFraction=.02)
  sm.valid['wideRoadCameraState'] = sm.alive['wideRoadCameraState'] = True
  sm.recv_time['wideRoadCameraState'] = 10.
  update(10.)
  np.testing.assert_equal(viewer._meter_polygon, np.array(points).reshape(4, 2))
  assert 'FOV | dark 10% bright 2%' in viewer._exposure_text
  update(12.)
  assert viewer._meter_polygon is None and viewer._exposure_text == 'AE --'
  sm.recv_time['wideRoadCameraState'] = 14.
  sm['wideRoadCameraState'].pv5ExposureFovActive = False
  update(14.)
  assert viewer._meter_polygon is None and 'waiting/fallback' in viewer._exposure_text


@pytest.mark.parametrize('mode', range(4))
@pytest.mark.parametrize('wide', [False, True])
def test_meter_waiting_label_matches_the_selected_camera(preview, mode, wide):
  viewer, update, _, settings, _ = preview
  settings['Pv5ModelFovExposure'] = mode
  sm = viewer._test_state.sm
  name = 'wideRoadCameraState' if wide else 'roadCameraState'
  sm[name] = NS(sensor='os04c10', measuredGreyFraction=.2, targetGreyFraction=.1, gain=2., pv5ExposureFovActive=False)
  sm.valid[name] = sm.alive[name] = True
  sm.recv_time[name] = 10.
  update(10., road=not wide, wide=wide)
  selected = mode == 1 or mode == (3 if wide else 2)
  assert ('waiting/fallback' in viewer._exposure_text) is selected
  assert viewer.active and viewer.wide is wide


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('status,prefix', [(3, '정상 확인 중 1.2/2.0초'), (5, '보정 정보 만료'),
                                        (12, '영역 잘림'), (15, '반복 오류 잠금')])
def test_native_reason_replaces_generic_fallback_only_for_fresh_current_lens(preview, wide, status, prefix):
  viewer, update, _, settings, _ = preview
  settings['Pv5ModelFovExposure'] = 1
  sm = viewer._test_state.sm
  name = 'wideRoadCameraState' if wide else 'roadCameraState'
  sm[name] = NS(sensor='os04c10', measuredGreyFraction=.2, targetGreyFraction=.1, gain=2.,
                pv5ExposureFovActive=False, pv5ExposureFovStatus=status, pv5ExposureFovWarmupMs=1250)
  sm.valid[name] = sm.alive[name] = True
  sm.recv_time[name] = 10.
  update(10., road=not wide, wide=wide)
  assert viewer._exposure_status_text.startswith(prefix)
  assert 'STOCK' in viewer._exposure_text and 'waiting/fallback' not in viewer._exposure_text
  update(10.35, road=not wide, wide=wide)
  assert viewer._exposure_status_text == ''  # Expire without waiting for the .5s formatted-text poll.
  sm.recv_time[name] = 10.6
  update(10.6, road=not wide, wide=wide)
  assert viewer._exposure_status_text.startswith(prefix)
  update(12., road=True, wide=True)
  if viewer.wide != wide:
    assert viewer._exposure_status_text == ''  # No previous-lens reason crosses the 2s switch.


def test_preview_toggle_never_bypasses_invalid_vehicle_or_calibration(preview):
  viewer, update, car, settings, _ = preview
  settings['Pv5CameraFovPreview'] = True
  car.vEgoRaw = car.vEgo = 20.
  update(10., age=.31)
  assert not viewer.active
  update(10.3)
  assert viewer.active
  viewer._test_state.sm.valid['liveCalibration'] = False
  update(10.6)
  assert not viewer.active


@pytest.mark.parametrize("layout", ("onroad", "mici/onroad"))
def test_requesting_current_stream_cancels_pending_preview_switch(layout):
  source = Path(__file__).parents[1] / layout / "cameraview.py"
  tree = ast.parse(source.read_text(encoding="utf-8"))
  cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CameraView")
  method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "switch_stream")
  namespace = {"VisionStreamType": int}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
  view = NS(_stream_type=0, _switching=True, _target_stream_type=1, _target_client=object())
  namespace["switch_stream"](view, 0)
  assert not view._switching
  assert view._target_stream_type is None and view._target_client is None
