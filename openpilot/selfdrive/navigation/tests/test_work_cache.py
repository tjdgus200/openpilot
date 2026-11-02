import ast
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.selfdrive.navigation import mapsd, renderer
from openpilot.selfdrive.navigation.route import RouteTracker
from openpilot.selfdrive.navigation.tests.test_rendering import local_archive, make_archive


@pytest.mark.parametrize('active,nav,screen,web,jetlink,expected', [
  (False, False, 0, False, False, (False, False)),
  (True, False, 0, False, False, (False, False)),
  (True, True, 0, False, False, (True, False)),
  (True, None, 0, False, False, (True, False)),
  (False, False, 7, False, False, (True, True)),
  (False, False, 0, True, False, (True, True)),
  (False, False, 0, False, True, (True, False)),
])
def test_model_and_preview_demand(monkeypatch, active, nav, screen, web, jetlink, expected):
  params = NS(get_bool=lambda _: active, get=lambda _: nav, get_int=lambda _: screen)
  monkeypatch.setattr(mapsd, 'preview_fresh', lambda _: web)
  monkeypatch.setattr(mapsd, 'jetlink_map_demand', lambda **_: jetlink)
  assert mapsd.map_demand(params, now=10.) == expected


class UncachedRenderer(renderer.MapRenderer):
  def _merge_roads(self, tiles):
    return tuple(np.concatenate(column) for column in zip(*tiles, strict=True))

  def _project_route(self, route, position):
    return renderer.project(renderer.mercator(route), position)


def test_cached_images_bit_exact_with_moving_pose_and_inplace_route_changes():
  route = np.array([[0., -.001], [0., 0.], [.001, .001]])
  with local_archive(make_archive()) as archive:
    cached, original = renderer.MapRenderer(archive), UncachedRenderer(archive)
    for i in range(20):
      if i == 10:
        route[2, 0] = -.001
      position = renderer.Position(i * .00001, i * .000002, i * 19.)
      actual, expected = cached.render(position, route), original.render(position, route)
      assert (actual.valid, actual.complete, actual.road_distance) == (expected.valid, expected.complete, expected.road_distance)
      np.testing.assert_array_equal(actual.image, expected.image)
    assert cached._merged is not None
    assert not np.shares_memory(cached._route, route)


def test_tiles_replaced_missing_and_large_views_invalidate():
  with local_archive(make_archive()) as archive:
    cached = renderer.MapRenderer(archive)
    tile = cached.roads(0, 0)
    first = cached._merge_roads([tile])
    assert cached._merge_roads([tile]) is first
    replacement = tuple(a.copy() for a in tile)
    for a in replacement:
      a.flags.writeable = False
    second = cached._merge_roads([replacement])
    assert second is not first
    assert len(cached._merge_roads([tile, replacement])[0]) == 2 * len(tile[0])
    # Writable sources must not be cached by object identity.
    writable = tuple(a.copy() for a in tile)
    cached._merge_roads([writable])
    assert cached._merged is None
    huge = (np.zeros((600_000, 2)),)
    huge[0].flags.writeable = False
    cached._merge_roads([huge])
    assert cached._merged is None


def test_route_tracker_owns_fixed_geometry():
  route = np.array([[127., 37.], [127., 37.01]])
  tracker = RouteTracker(route)
  expected = tracker.update_pose(127., 37.001)
  route[:] = 0
  assert tracker.update_pose(127., 37.001) == expected
  assert not np.shares_memory(tracker.route, route)
  with pytest.raises(ValueError):
    tracker.route[:] = 0


def test_large_routes_are_not_retained():
  with local_archive(make_archive()) as archive:
    cached = renderer.MapRenderer(archive)
    route = np.zeros((4097, 2))
    cached._project_route(route, renderer.Position(0, 0, 0))
    assert cached._route is None and cached._route_mercator is None


def test_route_dtype_change_does_not_reuse_rounded_projection():
  with local_archive(make_archive()) as archive:
    cached = renderer.MapRenderer(archive)
    route = np.array([[127., 37.], [127., 37.001]], dtype=np.float32)
    position = renderer.Position(37., 127., 10.)
    cached._project_route(route, position)
    widened = route.astype(np.float64)
    actual = cached._project_route(widened, position)
    np.testing.assert_array_equal(actual, renderer.project(renderer.mercator(widened), position))
    assert cached._route.dtype == np.float64


def test_model_transitions_publish_loaded_layout_before_activation():
  source = Path(__file__).parents[2] / 'modeld/modeld.py'
  tree = ast.parse(source.read_text())
  nav_writes, active_writes = [], []
  for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'put_bool' and node.args:
      if isinstance(node.args[0], ast.Constant):
        if node.args[0].value == 'UsbGpuNavModel':
          nav_writes.append(node.lineno)
        if node.args[0].value == 'UsbGpuActive':
          active_writes.append(node.lineno)
  assert len(nav_writes) == len(active_writes) == 4
  for line in active_writes:
    assert any(abs(line - n) == 1 for n in nav_writes)


@pytest.mark.parametrize('nav,preview', [(False, False), (True, False), (False, True)])
def test_mapsd_loop_preserves_model_frames_and_encodes_only_for_preview(nav, preview):
  # Run the production loop body with bounded synthetic transports/clock. No
  # source pattern assertion substitutes for checking actual frame publication.
  tree = ast.parse(Path(mapsd.__file__).read_text())
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  main.body = [n for n in main.body if not isinstance(n, ast.Import | ast.ImportFrom)]
  clock, sent, rendered, encoded = [10.], [], [], []

  class Finished(Exception):
    pass

  class SM(dict):
    updated = {'navRoute': True}
    valid = {'navRoute': True, 'navInstructionCarrot': True}
    recv_time = {'navInstructionCarrot': 10.}

    def update(self, _):
      self.recv_time['navInstructionCarrot'] = clock[0]
      # Loss of instruction must invalidate even between render deadlines.
      self.valid['navInstructionCarrot'] = clock[0] < 11.8

  sm = SM(navRoute=NS(carrotRouteId=1, coordinates=[NS(longitude=0., latitude=0.), NS(longitude=0., latitude=.01)]))

  class RK:
    frame = 0

    def __init__(self, *_, **__):
      pass

    def keep_time(self):
      self.frame += 1
      clock[0] = 10. + self.frame * .1
      if self.frame >= 30:
        raise Finished

  class Renderer:
    def __init__(self, _):
      pass

    def render(self, position, route):
      rendered.append(clock[0])
      return NS(valid=True, image=np.full((256, 256), 128, np.uint8))

  namespace = dict(vars(mapsd))
  namespace.update(
    os=NS(environ={}), time=NS(monotonic=lambda: clock[0], process_time=lambda: 0.),
    messaging=NS(SubMaster=lambda _: sm), Params=lambda: NS(get=lambda _: '', get_bool=lambda _: False),
    VisionIpcServer=lambda _: NS(create_buffers_with_sizes=lambda *a: None, start_listener=lambda: None,
                               send=lambda *args: sent.append(args)), VisionStreamType=NS(VISION_STREAM_MAP=1),
    map_demand=lambda *a, **kw: (nav or preview, preview), Ratekeeper=RK,
    RuntimeDiagnostics=lambda *a, **kw: NS(enabled=False), cloudlog=NS(event=None),
    ParkedPosition=lambda: NS(update=lambda _: NS(longitude=0., latitude=.001, speed=0., time=clock[0]), held=False),
    PMTiles=lambda _: NS(close=lambda: None), RemoteTiles=lambda: None, MapRenderer=Renderer,
    write_preview=lambda *args, **kwargs: encoded.append(clock[0]),
  )
  exec(compile(ast.Module(body=[main], type_ignores=[]), str(mapsd.__file__), 'exec'), namespace)
  with pytest.raises(Finished):
    namespace['main']()
  assert bool(rendered) == bool(nav or preview)
  assert bool(encoded) == preview
  assert all(b - a >= .5 - 1e-9 for a, b in zip(encoded, encoded[1:], strict=False))
  assert all(args[-2] == 1 and args[-1] <= int(clock[0] * 1e9) for args in sent)
  assert sent[-1][1] == bytes(65536)  # instruction loss always publishes black
  if nav:
    assert any(args[1] == bytes([128]) * 65536 for args in sent)
