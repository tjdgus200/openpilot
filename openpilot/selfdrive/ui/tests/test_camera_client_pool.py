"""Actual pool and both CameraView classes, with deterministic camera/GL doubles."""
import ast
import gc
import weakref
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

UI = Path(__file__).resolve().parents[1]


class Stream(IntEnum):
  VISION_STREAM_ROAD = 0
  VISION_STREAM_DRIVER = 1
  VISION_STREAM_WIDE_ROAD = 2
  VISION_STREAM_MAP = 3


ROAD, WIDE = Stream.VISION_STREAM_ROAD, Stream.VISION_STREAM_WIDE_ROAD


def load_pool(factory, clock):
  path = UI / 'onroad/camera_client_pool.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
  env = {'VisionIpcClient': factory, 'VisionStreamType': Stream, 'time': NS(monotonic=lambda: clock[0])}
  exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), env)
  return env['CameraClientPool']


class CameraDouble:
  def __init__(self, owner, stream):
    self.owner, self.stream = owner, stream
    self.connected = False
    self.num_buffers = 0
    self.width = self.height = self.stride = 16
    self.uv_offset = 256
    self.frames = []
    self.connect_calls = 0
    self.generation = 1

  def is_connected(self):
    return self.connected

  def connect(self, blocking):
    assert blocking is False
    self.connect_calls += 1
    self.owner.before_connect(self)
    self.connected = not self.owner.fail
    self.num_buffers = 0 if self.owner.zero_buffers or not self.connected else 3
    self.generation = self.owner.generation
    return self.connected

  def recv(self, timeout_ms):
    assert timeout_ms == 0
    if not self.frames:
      return None
    frame = self.frames[-1]
    self.frames.clear()  # The production clients are conflated.
    if frame.generation != self.generation:
      self.connected = False
      return None
    return frame

  def put(self, number):
    frame = NS(number=number, generation=self.owner.generation, idx=0, fd=100 + self.stream,
               width=16, height=16, stride=16, uv_offset=256)
    self.frames.append(frame)
    return frame

  def available_streams(self, name, block):
    assert block is False
    return [ROAD, WIDE]


class Factory:
  def __init__(self):
    self.created = 0
    self.alive = weakref.WeakSet()
    self.fail = self.zero_buffers = False
    self.generation = 1
    self.before_connect = lambda client: None

  def __call__(self, name, stream, conflate):
    assert conflate is True
    self.created += 1
    client = CameraDouble(self, stream)
    self.alive.add(client)
    return client


@pytest.fixture
def setup_pool():
  clock, factory = [10.], Factory()
  pool = load_pool(factory, clock)('test-camera')
  yield pool, factory, clock
  pool.close()


def test_bounded_reuse_and_unread_frame_discard(setup_pool):
  pool, factory, _ = setup_pool
  for i in range(1000):
    stream = ROAD if i % 2 else WIDE
    client = pool.select(stream)
    assert pool.connect(stream)
    client.put(i * 2 + 1)
    assert pool.recv(stream) is None
    assert pool.recv(stream) is None
    expected = client.put(i * 2 + 2)
    assert pool.recv(stream) is expected
  assert factory.created == 2
  assert all(client.connect_calls == 1 for client in factory.alive)


def test_unknown_stream_never_allocates(setup_pool):
  pool, factory, _ = setup_pool
  with pytest.raises(ValueError): pool.select(999)
  assert factory.created == 0


@pytest.mark.parametrize('zero_buffers', [False, True])
def test_failed_connection_is_throttled_per_stream(setup_pool, zero_buffers):
  pool, factory, clock = setup_pool
  factory.fail, factory.zero_buffers = not zero_buffers, zero_buffers
  client = pool.select(ROAD)
  for _ in range(100): assert not pool.connect(ROAD)
  assert client.connect_calls == 1
  second = pool.select(WIDE)
  assert not pool.connect(WIDE) and second.connect_calls == 1
  clock[0] += .21
  factory.fail = factory.zero_buffers = False
  assert pool.connect(ROAD) and client.connect_calls == 2
  assert factory.created == 2


def test_reset_reimports_all_clients_without_new_subscribers(setup_pool):
  pool, factory, _ = setup_pool
  clients = {s: pool.select(s) for s in (ROAD, WIDE)}
  for stream in clients: assert pool.connect(stream)
  pool.reset()
  for stream, client in clients.items():
    assert not pool.connected(stream)
    assert pool.connect(stream)
    assert client.connect_calls == 2
    client.put(1)
    assert pool.recv(stream) is None
  assert factory.created == 2


def test_close_releases_pool_references_and_disallows_new_clients(setup_pool):
  pool, factory, _ = setup_pool
  ref = weakref.ref(pool.select(ROAD))
  pool.select(WIDE)
  pool.close()
  pool.close()
  gc.collect()
  assert ref() is None and len(factory.alive) == 0
  with pytest.raises(RuntimeError): pool.select(ROAD)


def load_view(relative, factory, clock, tici):
  path = UI / relative
  tree = ast.parse(path.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CameraView')
  tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
  live_textures, live_shaders, images, calls = set(), set(), set(), []
  sequence = [0]
  def resource(ledger):
    sequence[0] += 1
    ledger.add(sequence[0])
    return NS(id=sequence[0], width=16, height=16)
  def release(ledger, value):
    assert value.id in ledger
    ledger.remove(value.id)
    calls.append('release')
  rl = NS(load_shader_from_memory=lambda *a: resource(live_shaders), get_shader_location=lambda *a: 0,
          unload_shader=lambda v: release(live_shaders, v),
          load_texture_from_image=lambda *a: resource(live_textures), unload_texture=lambda v: release(live_textures, v),
          gen_image_color=lambda *a: None, unload_image=lambda *a: None, BLACK=0,
          ffi=NS(new=lambda *a: [1]), Image=lambda *a: None, get_time=lambda: clock[0],
          PixelFormat=NS(PIXELFORMAT_UNCOMPRESSED_GRAYSCALE=0, PIXELFORMAT_UNCOMPRESSED_GRAY_ALPHA=1),
          Rectangle=lambda *a: NS(x=a[0], y=a[1], width=a[2], height=a[3]))
  state = NS(onroad=True, add_offroad_transition_callback=lambda f: None)
  state.is_onroad = lambda: state.onroad
  def destroy(value):
    assert value in images
    images.remove(value)
    calls.append('egl-release')
  env = dict(Widget=object, VisionIpcClient=factory, VisionStreamType=Stream, CameraClientPool=load_pool(factory, clock),
             rl=rl, np=np, TICI=tici, VERTEX_SHADER='', FRAME_FRAGMENT_SHADER='',
             ui_state=state, cloudlog=NS(debug=lambda *a: None), init_egl=lambda: True,
             destroy_egl_image=destroy, CONNECTION_RETRY_INTERVAL=.2)
  exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), env)
  view = env['CameraView']('test-camera', ROAD)
  # Exercise _render through the actual selection/connection/frame code; only GL draws are replaced.
  drawn = []
  view._render_egl = view._render_textures = lambda *a: drawn.append(view.frame)
  view._draw_placeholder = lambda *a: drawn.append(None)
  return NS(view=view, rl=rl, state=state, clock=clock, textures=live_textures, shaders=live_shaders,
            images=images, calls=calls, drawn=drawn)


@pytest.fixture(params=[(v, t) for v in ('onroad/cameraview.py', 'mici/onroad/cameraview.py') for t in (False, True)])
def scene(request):
  factory, clock = Factory(), [10.]
  s = load_view(request.param[0], factory, clock, request.param[1])
  s.factory, s.clock, s.tici = factory, clock, request.param[1]
  yield s
  s.view.close()
  assert not s.textures and not s.shaders and not s.images


def show_current(s, number):
  assert s.view._ensure_connection()
  s.view._render(s.rl.Rectangle(0, 0, 100, 100))  # discard barrier
  expected = s.view.client.put(number)
  s.view._render(s.rl.Rectangle(0, 0, 100, 100))
  assert s.view.frame is expected and s.drawn[-1] is expected


def finish_switch(s, stream, number):
  view = s.view
  view.switch_stream(stream)
  client = view._target_client
  client.put(number - 1)  # queued inactive image is not displayed
  view._handle_switch()
  assert view.stream_type != stream
  expected = client.put(number)
  view._handle_switch()
  assert view.stream_type == stream and view.frame is expected
  return client


def test_repeated_switches_keep_two_clients_and_no_old_frame(scene):
  s = scene
  show_current(s, 1)
  road = s.view.client
  for i in range(100):
    finish_switch(s, WIDE, 4 * i + 3)
    assert finish_switch(s, ROAD, 4 * i + 5) is road
  assert s.factory.created == 2


def test_pending_switch_can_cancel_or_change_target(scene):
  s = scene
  show_current(s, 1)
  old = s.view.frame
  s.view.switch_stream(WIDE)
  wide = s.view._target_client
  s.view.switch_stream(WIDE)
  assert s.view._target_client is wide
  s.view.switch_stream(ROAD)
  assert not s.view._switching and s.view.frame is old
  finish_switch(s, WIDE, 3)
  assert s.view.client is wide and s.factory.created == 2


def test_offroad_transition_cancels_target_and_drops_old_frames(scene):
  s = scene
  show_current(s, 1)
  s.view.switch_stream(WIDE)
  target = s.view._target_client
  s.state.onroad = False
  s.view._offroad_transition()
  assert s.view.frame is None and not s.view._switching and s.view._target_client is None
  s.state.onroad = True
  s.view._offroad_transition()
  old = s.view.client
  s.factory.before_connect = lambda c: (pytest.fail('old EGL import alive') if s.images else None)
  old.put(2)
  assert s.view._ensure_connection()
  s.view._render(s.rl.Rectangle(0, 0, 100, 100))
  assert s.view.frame is None
  show_current(s, 3)
  finish_switch(s, WIDE, 5)
  assert s.view.client is target and s.factory.created == 2


def test_restart_reconnects_same_client_and_releases_gl_before_import(scene):
  s = scene
  show_current(s, 1)
  client = s.view.client
  if s.tici:
    s.images.add(42)
    s.view.egl_images[0] = 42
  s.factory.generation = 2
  client.put(2)  # native recv marks disconnected when server_id changes
  s.view._render(s.rl.Rectangle(0, 0, 100, 100))
  assert s.view.frame is None
  def before(c):
    assert not s.images and s.view.frame is None
    assert s.view.texture_y is None and s.view.texture_uv is None
  s.factory.before_connect = before
  s.clock[0] += .21
  assert s.view._ensure_connection()
  assert client is s.view.client and client.connect_calls == 2
  show_current(s, 3)
  assert s.factory.created == 1


def test_failed_target_preserves_current_display_and_bounds_retries(scene):
  s = scene
  show_current(s, 1)
  old = s.view.frame
  s.factory.fail = True
  s.view.switch_stream(WIDE)
  target = s.view._target_client
  for _ in range(100): s.view._handle_switch()
  assert target.connect_calls == 1 and s.view.frame is old and s.view.stream_type == ROAD
  s.factory.fail = False
  s.clock[0] += .21
  s.view._handle_switch()
  target.put(3)
  s.view._handle_switch()
  assert s.view.stream_type == WIDE


def test_close_during_pending_switch_is_idempotent_and_frees_every_client(scene):
  s = scene
  show_current(s, 1)
  s.view.switch_stream(WIDE)
  refs = [weakref.ref(c) for c in s.factory.alive]
  s.view.close()
  s.view.close()
  gc.collect()
  assert all(ref() is None for ref in refs)
  assert not s.view._switching and s.view.frame is None
