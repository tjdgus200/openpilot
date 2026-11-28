"""Display cadence, urgent invalidation and nested render-target contracts."""
import importlib.util
import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock  # noqa: TID251 - fake graphics API, no unittest test runner

import pytest


@pytest.fixture
def layer_env(monkeypatch):
  app = SimpleNamespace(_scaled_width=2160, _scaled_height=1080, width=2160, height=1080, frame=0, render_framebuffer=73)
  rl = Mock()
  rl.Rectangle.side_effect = lambda x, y, width, height: SimpleNamespace(x=x, y=y, width=width, height=height)
  rl.Vector2.side_effect = lambda x, y: SimpleNamespace(x=x, y=y)
  active = [73]  # caller may itself be drawing into a recording texture
  rl.rl_get_active_framebuffer.side_effect = lambda: active[0]
  rl.rl_enable_framebuffer.side_effect = lambda value: active.__setitem__(0, value)

  def allocate(*size):
    active[0] = 0  # raylib allocation unbinds the caller's FBO
    return SimpleNamespace(id=84, texture=SimpleNamespace(id=85))

  rl.load_render_texture.side_effect = allocate
  monkeypatch.setitem(sys.modules, 'pyray', rl)
  monkeypatch.setitem(sys.modules, 'openpilot.system.ui.lib.application', SimpleNamespace(gui_app=app))
  source = Path(__file__).parents[3] / 'system/ui/lib/overlay_cache.py'
  spec = importlib.util.spec_from_file_location('_overlay_under_test', source)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  clock = SimpleNamespace(now=1.0)
  module.time = SimpleNamespace(monotonic=lambda: clock.now)
  return module, app, rl, active, clock


@pytest.fixture
def road_view(layer_env):
  module, app, rl, active, clock = layer_env
  rl.unload_render_texture.side_effect = lambda _: active.__setitem__(0, 0)
  source = Path(__file__).parents[1] / 'onroad/augmented_road_view.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AugmentedRoadView')
  cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ('set_cluster_hud_connected', '_render')]

  class CameraView:
    def _render(self, rect):
      self.camera(rect)

  ui = SimpleNamespace(started=True, CP=None, sm={})
  app.is_recording = lambda: False
  namespace = {'CameraView': CameraView, 'ui_state': ui, 'gui_app': app, 'rl': rl, 'time': SimpleNamespace(monotonic=lambda: clock.now),
               'ROAD_CAM': 0, 'WIDE_CAM': 1, 'UI_BORDER_SIZE': 12, 'render_dashboard': Mock(), 'messaging': Mock()}
  exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), namespace)
  view = namespace['AugmentedRoadView']()
  for name in ('camera', '_refresh_plot_mode', '_switch_stream_if_needed', '_update_calibration', '_draw_border_carrot',
               'alert_renderer', 'driver_state_renderer', '_pm', '_overlay_state', 'client'):
    setattr(view, name, Mock())
  view.stream_type = 0
  view.available_streams = {0, 1}
  view._suppress_camera_for_cluster = False
  view._fov_preview = SimpleNamespace(active=False, update=Mock())
  view._render_diagnostics = SimpleNamespace(enabled=False, start=Mock(), finish=Mock(), call=lambda _, draw, *args: draw(*args))
  view._plot_mode = 0
  view.model_renderer = SimpleNamespace(_overlay=module.OverlayLayer(0))
  view._hud_renderer = SimpleNamespace(_overlay=module.OverlayLayer(1), set_driving_feedback_mode=Mock())
  for renderer in (view.model_renderer, view._hud_renderer):
    renderer.paint = Mock()
    renderer.render = Mock(side_effect=lambda rect, r=renderer: r._overlay.draw(rect, r.paint, 'steady'))
  return view, ui


@pytest.mark.parametrize('show_camera,connected', ((True, True), (False, False)))
def test_pv5_hidden_model_cache_released_and_painted_on_restore(layer_env, road_view, show_camera, connected):
  _, app, rl, active, clock = layer_env
  view, ui = road_view
  ui.CP = SimpleNamespace(carFingerprint='KIA_PV5', notCar=False)
  model = view.model_renderer
  hud = view._hud_renderer
  rect = rl.Rectangle(0, 0, 2160, 1080)
  view.set_cluster_hud_connected(True, show_camera=True)
  view._render(rect)
  model_target, hud_target = model._overlay._target, hud._overlay._target
  assert model_target is not None and hud_target is not None
  rl.unload_render_texture.assert_not_called()

  view.set_cluster_hud_connected(True)
  assert model._overlay._target is None
  rl.unload_render_texture.assert_called_once()
  assert rl.unload_render_texture.call_args.args[0] is model_target
  assert active[0] == app.render_framebuffer
  for _ in range(3):
    view.set_cluster_hud_connected(True)
    view._render(rect)
  assert model.render.call_count == view.camera.call_count == 1
  assert hud.render.call_count == view.alert_renderer.render.call_count == view.driver_state_renderer.draw_onroad.call_count == 4
  rl.unload_render_texture.assert_called_once()
  assert rl.load_render_texture.call_count == 2
  assert hud._overlay._target is hud_target

  app.frame = 1  # The model layer's odd frame normally reuses its previous paint.
  clock.now += .05
  view.set_cluster_hud_connected(connected, show_camera=show_camera)
  view._render(rect)
  assert model.render.call_count == view.camera.call_count == model.paint.call_count == 2
  assert model._overlay._target is not model_target
  assert hud._overlay._target is hud_target
  assert rl.load_render_texture.call_count == 3
  assert active[0] == 73


@pytest.mark.parametrize('cp', (None, SimpleNamespace(carFingerprint='KIA_PV5', notCar=True),
                                SimpleNamespace(carFingerprint='OTHER', notCar=False)))
def test_hidden_model_cache_retained_outside_real_pv5(layer_env, road_view, cp):
  _, _, rl, _, _ = layer_env
  view, ui = road_view
  ui.CP = cp
  rect = rl.Rectangle(0, 0, 2160, 1080)
  view._render(rect)
  model = view.model_renderer
  target = model._overlay._target
  view.set_cluster_hud_connected(True)
  view._render(rect)
  assert model._overlay._target is target
  assert model.render.call_count == 1
  rl.unload_render_texture.assert_not_called()


def test_layers_alternate_and_composite_every_camera_frame(layer_env):
  module, app, rl, active, clock = layer_env
  layers = [module.OverlayLayer(0), module.OverlayLayer(1)]
  paints = [[], []]
  rect = rl.Rectangle(0, 0, 2160, 1080)
  for frame in range(20):
    app.frame = frame
    clock.now = 1.0 + frame / 20
    for i, layer in enumerate(layers):
      layer.draw(rect, lambda _, i=i, frame=frame: paints[i].append(frame), 'steady')
      assert active[0] == 73
  assert paints == [list(range(0, 20, 2)), [0] + list(range(1, 20, 2))]
  assert rl.draw_texture_pro.call_count == 40
  assert rl.load_render_texture.call_count == 2


def test_key_urgent_and_resume_never_reuse_old_state(layer_env):
  module, app, rl, _, clock = layer_env
  layer = module.OverlayLayer(0)
  rect = rl.Rectangle(0, 0, 100, 100)
  paint = Mock()
  layer.draw(rect, paint, 'red')
  app.frame = 1
  clock.now += .05
  layer.draw(rect, paint, 'green')  # wrong phase, but transition is immediate
  assert paint.call_count == 2
  layer.draw(rect, paint, 'green', urgent=True)
  assert paint.call_count == 3
  assert rl.draw_texture_pro.call_count == 2
  layer.draw(rect, paint, 'green')  # first frame after warning/touch must be fresh
  assert paint.call_count == 4


@pytest.mark.parametrize('elapsed', (.101, -.01))
def test_age_limit_and_clock_reset_force_refresh(layer_env, elapsed):
  module, app, rl, _, clock = layer_env
  layer = module.OverlayLayer(0)
  paint = Mock()
  rect = rl.Rectangle(0, 0, 100, 100)
  layer.draw(rect, paint, None)
  app.frame = 1
  clock.now += elapsed
  layer.draw(rect, paint, None)
  assert paint.call_count == 2


def test_resize_and_layout_force_fresh_layer(layer_env):
  module, _, rl, active, _ = layer_env
  layer = module.OverlayLayer(0)
  paint = Mock()
  rect = rl.Rectangle(0, 0, 100, 100)
  layer.draw(rect, paint, None)
  rect.x = 50
  layer.draw(rect, paint, None)
  module.gui_app._scaled_width = 1080
  module.gui_app._scaled_height = 540
  layer.draw(rect, paint, None)
  assert paint.call_count == 3
  assert rl.load_render_texture.call_count == 2
  assert rl.unload_render_texture.call_count == 1
  assert active[0] == 73
  assert layer._source.height == -540
  assert layer._destination.width == 2160  # caller's scale transform stays in force


def test_failed_allocation_falls_back_without_repeated_allocations(layer_env):
  module, _, rl, active, _ = layer_env
  rl.load_render_texture.side_effect = lambda *size: SimpleNamespace(id=0, texture=SimpleNamespace(id=0))
  layer = module.OverlayLayer(0)
  paint = Mock()
  for _ in range(3):
    layer.draw(rl.Rectangle(0, 0, 100, 100), paint, None)
  assert paint.call_count == 3
  assert rl.load_render_texture.call_count == 1
  assert rl.draw_texture_pro.call_count == 0
  assert active[0] == 73


def test_incomplete_nonzero_framebuffer_uses_direct_drawing(layer_env):
  module, _, rl, active, _ = layer_env
  rl.rl_framebuffer_complete.return_value = False
  layer = module.OverlayLayer(0)
  painted_targets = []
  for _ in range(3):
    layer.draw(rl.Rectangle(0, 0, 100, 100), lambda _: painted_targets.append(active[0]), None)
  assert painted_targets == [73, 73, 73]
  assert rl.load_render_texture.call_count == 1
  assert rl.draw_texture_pro.call_count == 0


def test_painter_exception_restores_callers_target(layer_env):
  module, _, rl, active, _ = layer_env
  layer = module.OverlayLayer(0)
  with pytest.raises(ValueError):
    layer.draw(rl.Rectangle(0, 0, 100, 100), Mock(side_effect=ValueError), None)
  assert active[0] == 73
  assert not layer._ready
  rl.end_blend_mode.assert_called_once()


def test_display_filter_has_same_time_constant_at_half_rate(layer_env):
  module, _, _, _, clock = layer_env
  values = []
  for period in (.05, .1):
    display = module.DisplayClock()
    display._previous = 0.0
    filt = SimpleNamespace(initialized=True, alpha=.2, x=0.0)
    for frame in range(1, round(1 / period) + 1):
      clock.now = frame * period
      display.tick()
      display.update_filter(filt, 100.0)
    values.append(filt.x)
  assert values[0] == pytest.approx(values[1])


def test_camera_and_critical_states_invalidate_both_layers(layer_env):
  _, app, _, _, _ = layer_env
  source = Path(__file__).parents[1] / 'overlay_state.py'
  spec = importlib.util.spec_from_file_location('_overlay_state_under_test', source)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  module.signal_display_status = lambda: None
  app.mouse_events = []
  cs = SimpleNamespace(leftBlindspot=False, rightBlindspot=False, vCruiseCluster=80, gearShifter=SimpleNamespace(raw=1),
                       brakePressed=False, gasPressed=False, brakeHoldActive=False, softHoldActive=False, carrotCruise=False)
  ss = SimpleNamespace(alertSize=SimpleNamespace(raw=0), alertHudVisual=SimpleNamespace(raw=0), enabled=True, experimentalMode=True)
  class Messages(dict):
    pass
  sm = Messages(carState=cs, selfdriveState=ss, longitudinalPlan=SimpleNamespace(xState=0, trafficState=1, allowThrottle=True),
                modelV2=SimpleNamespace(meta=SimpleNamespace(laneChangeState=SimpleNamespace(raw=0))),
                carrotMan=SimpleNamespace(trafficState=1), radarState=SimpleNamespace(
                  leadOne=SimpleNamespace(status=True, radarTrackId=12), leadTwo=SimpleNamespace(status=False)))
  services = ['modelV2', 'carState', 'radarState', 'longitudinalPlan', 'carrotMan', 'liveCalibration']
  sm.recv_frame = dict.fromkeys(services, 1)
  sm.valid = dict.fromkeys(services, True)
  sm.alive = dict.fromkeys(services, True)
  ui = SimpleNamespace(sm=sm, started=True, started_frame=1, status=1, usbgpu_active=False,
                       usbgpu_loading=False, usbgpu_startup_failed=False)
  camera = Mock()
  camera.is_connected.return_value = True
  state = module.OverlayState()
  state.update(ui, camera, 0, 0)
  assert state.enabled and not state.urgent
  old = state.key
  sm['longitudinalPlan'].trafficState = 2
  state.update(ui, camera, 0, 0)
  assert state.key != old

  for field in ('leftBlindspot', 'rightBlindspot'):
    setattr(cs, field, True)
    state.update(ui, camera, 0, 0)
    assert state.urgent
    setattr(cs, field, False)
  for field in ('alertSize', 'alertHudVisual'):
    getattr(ss, field).raw = 1
    state.update(ui, camera, 0, 0)
    assert state.urgent
    getattr(ss, field).raw = 0
  app.mouse_events = [object()]
  state.update(ui, camera, 0, 0)
  assert state.urgent
  app.mouse_events = []
  sm['modelV2'].meta.laneChangeState.raw = 1
  state.update(ui, camera, 0, 0)
  assert state.urgent
  sm['modelV2'].meta.laneChangeState.raw = 0
  old = state.key
  camera.is_connected.return_value = False
  state.update(ui, camera, 0, 0)
  assert state.key != old


@pytest.mark.parametrize('family', ('onroad', 'mici/onroad'))
def test_speed_popup_duration_does_not_depend_on_paint_cadence(layer_env, family):
  _, _, rl, _, clock = layer_env
  source = Path(__file__).parents[1] / family / 'hud_renderer.py'
  tree = ast.parse(source.read_text(encoding='utf8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudRenderer')
  methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and
             n.name in ('_draw_cruise_speed_animation', '_update_cruise_speed_animation')]
  constants = [n for n in tree.body if isinstance(n, ast.Assign) and
               any(isinstance(t, ast.Name) and t.id.startswith('CRUISE_SPEED_ANIMATION_') for t in n.targets)]
  draws = []
  namespace = {'rl': rl, 'COLORS': SimpleNamespace(CARROT_GREEN=rl.GREEN), 'time': SimpleNamespace(monotonic=lambda: clock.now),
               'draw_text_ui_style': lambda *a, **kw: draws.append((a[1], a[2], a[3]))}
  exec(compile(ast.Module(body=constants+methods, type_ignores=[]), str(source), 'exec'), namespace)
  midpoints = []
  for period in (.05, .1):
    renderer = SimpleNamespace(_cruise_speed_text_last='', _cruise_speed_animation_time=0,
                               _cruise_speed_animation_text='', _font_display=object(),
                               _txt_speed_bg=SimpleNamespace(width=350, height=150))
    clock.now = 10.0
    namespace['_update_cruise_speed_animation'](renderer, '80')
    rect = rl.Rectangle(0, 0, 2160, 1080)
    for i in range(round(.2 / period)+1):
      clock.now = 10.0 + i*period
      namespace['_draw_cruise_speed_animation'](renderer, rect)
    midpoints.append(draws[-1])
    before = len(draws)
    clock.now = 10.5
    namespace['_draw_cruise_speed_animation'](renderer, rect)
    assert len(draws) == before
    assert renderer._cruise_speed_animation_time == 0
  assert midpoints[0] == midpoints[1]
