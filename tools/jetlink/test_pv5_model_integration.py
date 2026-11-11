"""Cross-model PC contracts. Real geometry/parser/control helper/schema/HUD adapter;
camera DMA, GPU inference, CAN, clocks and settings are fixture boundaries.
"""
import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from jetlink.spec import ModelSpec
from openpilot.cereal import log
from openpilot.common import big_model_catalog as catalog
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import WIDE_MODEL_FOV_CHOICES, get_warp_matrix, wider_model_warp
from openpilot.selfdrive.controls.lib.pv5_lane_boundary import Pv5LaneBoundary
from openpilot.selfdrive.modeld.tests.test_blendful import frame as blendful_frame
from openpilot.selfdrive.modeld.fill_model_msg import PublishState, fill_model_msg
from openpilot.selfdrive.modeld.jetlink.contracts import parse_outputs
from openpilot.selfdrive.modeld.parse_model_outputs import Parser

ROOT = Path(__file__).resolve().parents[2]
MODELS = tuple(catalog.available())


@pytest.fixture(params=MODELS)
def model_spec(request):
  return ModelSpec.from_dict(catalog.model_spec(request.param))


def fov_loop():
  """Execute the production live-poll/calibration/transform statements verbatim."""
  path = ROOT / 'openpilot/selfdrive/modeld/modeld.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  def assignment(node, name):
    return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
  loop = next(n for n in ast.walk(main) if isinstance(n, ast.While) and
              any(assignment(child, 'wide_fov_changed') for child in n.body))
  start = next(i for i, n in enumerate(loop.body) if assignment(n, 'wide_fov_changed'))
  end = next(i for i, n in enumerate(loop.body) if assignment(n, 'traffic_convention'))
  transforms = next(n for n in loop.body if assignment(n, 'transforms'))
  return compile(ast.Module(body=[*loop.body[start:end], transforms], type_ignores=[]), str(path), 'exec')


@pytest.mark.parametrize('selection', range(5))
@pytest.mark.parametrize('moving', [False, True])
def test_each_model_receives_live_fov_only_in_wide_transform(model_spec, selection, moving, monkeypatch):
  sm = type('Messages', (dict,), {})(
    liveCalibration=NS(rpyCalib=[.01, -.02, .005], calStatus=log.LiveCalibrationData.Status.calibrated),
    deviceState=NS(deviceType='tici'), roadCameraState=NS(sensor='os04c10'),
    carState=NS(vEgo=20. if moving else 0., gearShifter='drive' if moving else 'park'))
  sm.updated, sm.seen = {'liveCalibration': True}, {'roadCameraState': True, 'deviceState': True}
  setting = [0]
  namespace = dict(np=np, log=log, DEVICE_CAMERAS=DEVICE_CAMERAS, get_warp_matrix=get_warp_matrix,
                   wider_model_warp=wider_model_warp, WIDE_MODEL_FOV_CHOICES=WIDE_MODEL_FOV_CHOICES,
                   sm=sm, params=NS(get_int=lambda key: setting[0]), loop_start=100., next_wide_fov_poll=0.,
                   wide_fov_index=0, camera_yaw_trim_deg=0., main_wide_camera=False, use_extra_client=True,
                   model=NS(vision_input_names=['img', 'big_img']))
  code = fov_loop()
  exec(code, namespace)
  narrow = namespace['transforms']['img'].copy()
  stock = namespace['transforms']['big_img'].copy()
  setting[0], namespace['loop_start'] = selection, 100.26
  sm.updated['liveCalibration'] = False  # Setting alone must update, without a new calibration.
  exec(code, namespace)
  expected = wider_model_warp(namespace['stock_transform_extra'], WIDE_MODEL_FOV_CHOICES[selection-1]) if selection else stock
  np.testing.assert_array_equal(namespace['transforms']['img'], narrow)
  np.testing.assert_array_equal(namespace['transforms']['big_img'], expected)
  assert model_spec.warped_shape == (2, 6, 128, 256)
  assert model_spec.warped_nbytes == 393216
  if selection:
    assert not np.array_equal(expected, stock)
  # Execute the production Warp/JoiningModel path, with only camera/GPU/USB
  # replaced. The exact two matrices must reach submission before inference.
  from openpilot.selfdrive.modeld.jetlink import model as adapter
  from test_hotplug_resources import joining_model
  from test_input_overlap import fake_warp
  parse = adapter.parse_outputs
  joined = joining_model(monkeypatch)
  monkeypatch.setattr(adapter, 'parse_outputs', parse)
  joined.spec, joined.parser = model_spec, Parser()
  joined.packed = np.zeros(model_spec.packed_nelem, np.float32)
  joined.views = {key: array.reshape(shape) for (key, shape), array in zip(
    model_spec.packed_shapes.items(), np.split(joined.packed, np.cumsum(model_spec.packed_sizes[:-1])), strict=True)}
  warp, bufs, _ = fake_warp(monkeypatch, [])
  pixels = np.zeros(model_spec.warped_shape, np.uint8)
  warp.run_warp = lambda **kwargs: NS(numpy=lambda: pixels, shape=pixels.shape)
  joined.warp = warp
  calls = []
  def infer(image, packed, frame, reset, source_sof):
    np.testing.assert_array_equal(warp.transforms['tfm'], narrow)
    np.testing.assert_array_equal(warp.transforms['big_tfm'], expected)
    assert image.shape == model_spec.warped_shape and image.dtype == np.uint8
    assert packed.size == model_spec.packed_nelem
    calls.append(frame)
    return np.zeros(model_spec.output_nelem, np.float32)
  joined.client = NS(image_buffer=lambda: None, infer=infer, timings=(0, 0, 0),
                     close=lambda: pytest.fail('unexpected fallback'))
  parsed = joined.run(bufs, namespace['transforms'], {
    'desire_pulse': np.zeros(8, np.float32), 'traffic_convention': np.array([1, 0], np.float32),
    'action_t': np.array([.1, .1], np.float32)}, False)
  assert parsed['plan'].shape == (1, 33, 15) and calls == [1]
  # Invalid settings return to stock instead of retaining the last experiment.
  setting[0], namespace['loop_start'] = 999, 100.52
  exec(code, namespace)
  np.testing.assert_array_equal(namespace['transforms']['big_img'], stock)


def model_message(spec):
  parsed = parse_outputs(Parser(), spec, np.zeros(spec.output_nelem, np.float32))
  event = log.Event.new_message()
  event.init('modelV2')
  action = log.ModelDataV2.Action.new_message(desiredAcceleration=0., desiredCurvature=0.)
  fill_model_msg(event, parsed, action, PublishState(), 1, 1, 1, 0., 123, .04, True)
  return event


def eligible_model(spec, offset):
  # Synthetic clear-lane geometry exercises admission; it is not a road prediction.
  event = model_message(spec)
  md = event.modelV2
  md.position.x, md.position.y = [0., 20., 60.], [0.] * 3
  md.laneLineProbs, md.laneLineStds = [0., .99, .99, 0.], [0., .1, .1, 0.]
  for i, y in ((1, offset-1.7), (2, offset+1.7)):
    md.laneLines[i].x, md.laneLines[i].y = [0., 20., 60.], [y] * 3
  for lead in md.leadsV3:
    lead.prob = 0.
  md.meta.laneChangeState, md.meta.desire = 'off', 'none'
  md.meta.autoTurnSignal = 'none'
  return event


def center_scenario(spec, offset):
  # All five real parsers produce untouched model messages; correction is now
  # applied only to the final chosen control path, not injected into modeld.
  parsed = parse_outputs(Parser(), spec, np.zeros(spec.output_nelem, np.float32))
  geometry = blendful_frame(center=offset, probs=(.99, .99))
  for key in ('lane_lines', 'lane_lines_stds', 'lane_lines_prob'):
    parsed[key] = geometry[key]
  parsed['plan'][0, :, :3] = geometry['plan'][0]
  enabled = [True]
  assist = Pv5LaneBoundary(NS(get_bool=lambda _: enabled[0]))
  cs_event = log.Event.new_message(carState={'canValid': True, 'gearShifter': 'drive', 'vEgo': 20.})
  sm = type('Messages', (dict,), {})(carState=cs_event.carState,
       radarState=log.RadarState.new_message(), driverMonitoringState=log.DriverMonitoringState.new_message(),
       lateralPlan=log.LateralPlan.new_message(), selfdriveState=log.SelfdriveState.new_message(), modelV2=None)
  sm.seen = sm.valid = sm.alive = dict.fromkeys(sm, True)
  sm.recv_frame = {'modelV2': 0}
  for count in range(120):
    now = 100. + count*.05
    event = log.Event.new_message()
    event.init('modelV2')
    sm['modelV2'] = event.modelV2
    action = log.ModelDataV2.Action.new_message(desiredCurvature=0.)
    fill_model_msg(event, parsed, action, PublishState(), count+1, count+1, count+1, 0., 123, .04, True)
    sm.recv_time = dict.fromkeys(sm, now)
    original_action = event.modelV2.action.to_dict()
    correction = assist.update(sm, cs_event.carState, event.modelV2, now, active=True, curvature=0.)
    assert event.modelV2.action.to_dict() == original_action
  assert assist.status == 'correcting'
  assert np.copysign(1., offset)*correction > 0
  return assist, enabled, sm, event, cs_event, now, correction


@pytest.mark.parametrize('offset', [-.65, .65])
def test_each_model_corrects_both_directions_and_obeys_live_off(model_spec, offset):
  assist, enabled, sm, event, cs_event, now, _ = center_scenario(model_spec, offset)
  enabled[0] = False
  now += .26
  sm.recv_time = dict.fromkeys(sm, now)
  assert assist.update(sm, cs_event.carState, event.modelV2, now, active=True, curvature=0.) == 0.
  assert assist.status == 'off' and assist.fraction == 0.


@pytest.mark.parametrize('context', ['bsd', 'dm', 'lead'])
def test_each_model_has_no_added_obstacle_or_dm_policy(model_spec, context):
  assist, _, sm, event, cs_event, now, _ = center_scenario(model_spec, .65)
  if context == 'bsd':
    cs_event.carState.leftBlindspot = True
  elif context == 'dm':
    sm['driverMonitoringState'].activePolicy = 'vision'
    sm['driverMonitoringState'].visionPolicyState.isDistracted = True
  else:
    sm['radarState'].leadOne.status = True
    sm['radarState'].leadOne.dRel = 10.
  now += .05
  event.modelV2.frameId += 1
  sm.recv_time = dict.fromkeys(sm, now)
  correction = assist.update(sm, cs_event.carState, event.modelV2, now, active=True, curvature=0.)
  assert .08 < correction*400 <= .30
  assert assist.status == 'correcting' and assist.accel == pytest.approx(correction*400)


@pytest.mark.parametrize('context', ['stale', 'inactive', 'signal'])
def test_each_model_unapplied_or_stale_gauge_does_not_change_action(model_spec, context):
  assist, _, sm, event, cs_event, now, _ = center_scenario(model_spec, .65)
  action = event.modelV2.action.to_dict()
  now += .05
  event.modelV2.frameId += 1
  sm.recv_time = dict.fromkeys(sm, now)
  if context == 'stale': sm.recv_time['modelV2'] -= .3
  if context == 'signal': cs_event.carState.leftBlinker = True
  assist.update(sm, cs_event.carState, event.modelV2, now, active=context != 'inactive', curvature=0.)
  assert event.modelV2.action.to_dict() == action and assist.fraction == 0.


def publish_center(assist, correction, speed):
  path = ROOT / 'openpilot/selfdrive/controls/controlsd.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  block = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and
               'pv5_lane_center is not None' in ast.unparse(n.test) and
               'cs.pv5LaneCenterFraction' in ast.unparse(n))
  event = log.Event.new_message(controlsState={})
  namespace = {'self': NS(pv5_lane_center=assist, pv5_lane_center_accel=correction*speed**2),
               'cs': event.controlsState, 'unused_scale': 1.}
  exec(compile(ast.Module(body=[block], type_ignores=[]), str(path), 'exec'), namespace)
  return event


def test_each_model_center_and_lanes_survive_hud_wire_and_stale_clear(model_spec, monkeypatch):
  import hud
  import hud_protocol
  from cluster_live import OpenpilotLiveSource, standby_state
  from hud_health import HealthRenderer, ClusterUiRenderer

  assist, _, sm, md, car, now, correction = center_scenario(model_spec, .65)
  controls = publish_center(assist, correction, 20.)
  controls.controlsState.lateralControlState.init('angleState')
  controls.controlsState.desiredCurvature = .002
  events = {'modelV2': md, 'carState': car, 'controlsState': controls,
            'deviceState': log.Event.new_message(deviceState={'started': True}),
            'carControl': log.Event.new_message(carControl={'latActive': True})}
  record = dict(version=1, sent=now, params={}, valid=dict.fromkeys(events, True),
                alive=dict.fromkeys(events, True), received=dict.fromkeys(events, now),
                mono=dict.fromkeys(events, int(now*1e9)),
                events={key: base64.b64encode(event.to_bytes()).decode() for key, event in events.items()})
  decoded = json.loads(hud_protocol.encode_snapshot(record))
  snapshot = [(now, decoded)]
  monkeypatch.setattr(hud, 'read_snapshot', lambda: snapshot[0])
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now)
  remote = hud.RemoteSubMaster(list(events))
  remote.update()
  source = object.__new__(OpenpilotLiveSource)
  source.sm, source.params = remote, None
  source._next_egpu_param_read_t = float('inf')
  source._max_lateral_accel, source._energy_gauge_label = 3., 'battery'
  source._carrot_navi_media = None
  source._current_carrot_navi = lambda _: None
  source._live_cluster_alert = lambda *_: None
  state = source._with_live_hud_state(standby_state())
  assert state.pv5_lane_center_status == 'correcting'
  assert state.pv5_lane_center_fraction == pytest.approx(assist.fraction)
  assert state.left_lane_probability == pytest.approx(.99)
  assert state.right_lane_probability == pytest.approx(.99)
  assert state.steering_output_normalized == pytest.approx(.002 * 20**2 / 3.)
  renderer = object.__new__(HealthRenderer)
  renderer.language = 'ko'
  renderer._draw_host_telemetry = lambda *_: None
  renderer._ellipsize_text = lambda text, *_: text
  texts = []
  renderer._draw_text = lambda text, *_args, **_kwargs: texts.append(text)
  monkeypatch.setattr(ClusterUiRenderer, '_draw_steering_output_block', lambda *_: None)
  import hud_health
  monkeypatch.setattr(hud_health.rl, 'draw_rectangle', lambda *_: None)
  renderer._draw_steering_output_block(state)
  assert any('차선 보정' in text and '›' in text for text in texts)
  snapshot[0] = None
  remote.update()
  stale = source._with_live_hud_state(state)
  assert stale.pv5_lane_center_status == 'off' and stale.pv5_lane_center_fraction == 0.
  assert stale.left_lane_probability is stale.right_lane_probability is None
