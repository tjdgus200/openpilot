"""Test the parked preview with real camera geometry and synthetic vehicle state."""
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
  settings = {"Pv5WideModelFov": 4, "CameraYawTrimDeg": 0, "Pv5FovPreviewPitch": 0, "UseWideCamera": True}
  sm = type("Messages", (dict,), {})(carState=car, deviceState=NS(deviceType="mici"),
                                    roadCameraState=NS(sensor="os04c10"),
                                    liveCalibration=NS(calStatus=calibrated, rpyCalib=[0., 0., 0.]))
  sm.valid = {"carState": True, "liveCalibration": True}
  sm.alive = {"carState": True, "liveCalibration": True}
  sm.seen = {"roadCameraState": True, "deviceState": True}
  sm.recv_time = {"carState": 10.}
  sm.recv_frame = {"liveCalibration": 1}
  state = NS(started=True, CP=NS(notCar=False, carFingerprint="KIA_PV5"), sm=sm,
             params=NS(get_int=settings.__getitem__, get_float=settings.__getitem__,
                       get=lambda key, **_: settings[key]))
  viewer = namespace["StoppedFovPreview"]()

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
