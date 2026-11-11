"""Actual wide-input geometry and live modeld statements; no vehicle/GPU safety claim."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import (WIDE_MODEL_DOWN_MAX, WIDE_MODEL_FOV_CHOICES,
  get_warp_matrix, wide_model_pitch, wider_model_warp)

ROOT = Path(__file__).resolve().parents[2]
CORNERS = np.array([[0, 511, 511, 0], [0, 0, 255, 255], [1, 1, 1, 1]])


def polygon(warp):
  rays = warp @ CORNERS
  assert np.isfinite(rays).all() and (rays[2] > 0).all()
  return (rays[:2] / rays[2]).T


@pytest.mark.parametrize('selection', range(5))
@pytest.mark.parametrize('rpy', [(0., 0., 0.), (.04, -.08, .03), (-.04, .08, -.03)])
def test_all_steps_move_every_corner_down_and_preserve_calibration(selection, rpy):
  rpy = np.array(rpy, np.float32)
  original = rpy.copy()
  lens = DEVICE_CAMERAS['tizi', 'ox03c10'].ecam
  previous = None
  for down in range(101):
    adjusted = wide_model_pitch(rpy, down)
    warp = get_warp_matrix(adjusted, lens.intrinsics, True)
    if selection:
      warp = wider_model_warp(warp, WIDE_MODEL_FOV_CHOICES[selection - 1])
    points = polygon(warp)
    if previous is not None:
      assert (points[:, 1] > previous[:, 1]).all()
    # Current C3X camera envelope, with these explicitly assumed calibrations.
    assert (points[:, 0] >= 0).all() and (points[:, 0] < lens.width).all()
    assert (points[:, 1] >= 0).all() and (points[:, 1] < lens.height).all()
    np.testing.assert_array_equal(rpy, original)
    np.testing.assert_array_equal(adjusted[[0, 2]], original[[0, 2]])
    previous = points


@pytest.mark.parametrize('bad', [-1, 101, 1000, 1.5, np.nan, True, None])
def test_geometry_rejects_invalid_offset(bad):
  with pytest.raises(ValueError):
    wide_model_pitch(np.zeros(3), bad)


def live_loop(fingerprint='KIA_PV5', not_car=False, dual=True):
  tree = ast.parse((ROOT / 'selfdrive/modeld/modeld.py').read_text())
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  def assigned(node, name):
    return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
  loop = next(n for n in ast.walk(main) if isinstance(n, ast.While) and any(assigned(c, 'wide_fov_changed') for c in n.body))
  start = next(i for i, n in enumerate(loop.body) if assigned(n, 'wide_fov_changed'))
  end = next(i for i, n in enumerate(loop.body) if assigned(n, 'traffic_convention'))
  code = compile(ast.Module(body=loop.body[start:end], type_ignores=[]), '<production modeld FOV>', 'exec')
  settings = {'Pv5WideModelFov': 4, 'Pv5WideModelDown': 0}
  sm = type('Messages', (dict,), {})(liveCalibration=NS(rpyCalib=[.01, -.02, .005], calStatus=1),
     deviceState=NS(deviceType='tizi'), roadCameraState=NS(sensor='ox03c10'))
  sm.updated = {'liveCalibration': True}
  sm.seen = {'roadCameraState': True, 'deviceState': True}
  state = dict(np=np, log=NS(LiveCalibrationData=NS(Status=NS(calibrated=1))),
    DEVICE_CAMERAS=DEVICE_CAMERAS, get_warp_matrix=get_warp_matrix, wider_model_warp=wider_model_warp,
    wide_model_pitch=wide_model_pitch, WIDE_MODEL_FOV_CHOICES=WIDE_MODEL_FOV_CHOICES, WIDE_MODEL_DOWN_MAX=WIDE_MODEL_DOWN_MAX,
    CP=NS(carFingerprint=fingerprint, notCar=not_car), sm=sm, params=NS(get_int=settings.__getitem__, get=settings.__getitem__),
    loop_start=10., next_wide_fov_poll=0., wide_fov_index=0, wide_model_down=0,
    camera_yaw_trim_deg=0., main_wide_camera=False, use_extra_client=dual)
  exec(code, state)
  sm.updated['liveCalibration'] = False
  return code, state, settings


@pytest.mark.parametrize('down', [1, 10, 30, 50, 99, 100, -1, 101, 999, None, 1.5, True])
@pytest.mark.parametrize('scope', ['pv5', 'other', 'notcar', 'single'])
def test_real_loop_live_poll_scope_restore_and_narrow_parity(down, scope):
  code, state, settings = live_loop('HYUNDAI_IONIQ_5' if scope == 'other' else 'KIA_PV5', scope == 'notcar', scope != 'single')
  narrow, wide = state['model_transform_main'].copy(), state['model_transform_extra'].copy()
  settings['Pv5WideModelDown'] = down
  state['loop_start'] += .1
  exec(code, state)  # Not due: the frame path must not rebuild/poll.
  np.testing.assert_array_equal(state['model_transform_extra'], wide)
  state['loop_start'] += .2
  exec(code, state)
  np.testing.assert_array_equal(state['model_transform_main'], narrow)
  applied = scope == 'pv5' and type(down) is int and 1 <= down <= 100
  assert np.array_equal(state['model_transform_extra'], wide) is not applied
  if applied:
    assert (polygon(state['model_transform_extra'])[:, 1] > polygon(wide)[:, 1]).all()
  settings['Pv5WideModelDown'] = 0
  state['loop_start'] += .3
  exec(code, state)
  np.testing.assert_array_equal(state['model_transform_extra'], wide)


def test_setting_is_persistent_default_zero_and_separate_from_preview():
  import json
  params = {p['name']: p for p in json.loads((ROOT / 'selfdrive/carrot_settings.json').read_text())['params']}
  p = params['Pv5WideModelDown']
  assert (p['default'], p['min'], p['max'], p['unit']) == (0, 0, 100, 1)
  assert p['risk'] == 'high'
  assert '{"Pv5WideModelDown", {PERSISTENT, INT, "0"}}' in (ROOT / 'common/params_keys.h').read_text()
  source = (ROOT / 'selfdrive/modeld/modeld.py').read_text()
  assert 'Pv5FovPreviewPitch' not in source
