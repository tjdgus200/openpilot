"""Check that model daylight metadata stays scoped to a fresh onroad camera."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.ui.tests.test_camera_client_pool import Stream


class ModelSubscriber:
  def __init__(self):
    self.valid = {"modelV2": True}
    self.recv_frame = {"modelV2": 42}
    self.recv_time = {"modelV2": 9.99}
    self.model = NS(timestampEof=19_950_000_000, daylightRoadExponent=0.5, daylightWideRoadExponent=0.75)

  def __getitem__(self, service):
    assert service == "modelV2"
    return self.model


@pytest.fixture
def fixture():
  updates, starts, loaded, unloaded = [], [], [], []
  def load_shader(vertex, fragment):
    loaded.append((vertex, fragment))
    return NS(id=1)
  rl = NS(load_shader_from_memory=load_shader,
          unload_shader=lambda shader: unloaded.append(shader.id),
          begin_shader_mode=lambda shader: starts.append(shader.id),
          get_shader_location=lambda *args: 4,
          ffi=NS(new=lambda kind: [0.0]),
          ShaderUniformDataType=NS(SHADER_UNIFORM_FLOAT=0),
          set_shader_value=lambda shader, loc, value, kind: updates.append((loc, value[0])))
  time = NS(CLOCK_BOOTTIME=7, monotonic=lambda: 10., clock_gettime_ns=lambda clock: 20_000_000_000)
  path = Path(__file__).parents[1] / "onroad/camera_daylight.py"
  tree = ast.parse(path.read_text(encoding="utf-8"))
  tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
  env = {"VisionStreamType": Stream, "time": time, "rl": rl}
  exec(compile(tree, str(path), "exec"), env)
  shader = env["Daylight75Shader"]("vertex", "\n#version 300 es\nfragment")
  return NS(exponent=env["daylight75_exponent"], shader=shader, init_shader=env["Daylight75Shader"],
            updates=updates, starts=starts, loaded=loaded, unloaded=unloaded, rl=rl,
            state=NS(started=True, started_frame=40, sm=ModelSubscriber(), show_model_daylight75=True), camera_eof=19_980_000_000)


def test_selected_physical_camera(fixture):
  f = fixture
  assert f.exponent(f.state, Stream.VISION_STREAM_ROAD, f.camera_eof) == 0.5
  assert f.exponent(f.state, Stream.VISION_STREAM_WIDE_ROAD, f.camera_eof) == 0.75
  assert f.exponent(f.state, Stream.VISION_STREAM_DRIVER, f.camera_eof) == 0.0
  assert f.exponent(f.state, 999, f.camera_eof) == 0.0


@pytest.mark.parametrize("value", [0., 1., 1.1, -0.5, 0.01, float("nan"), float("inf")])
def test_identity_and_invalid_exponents(fixture, value):
  f = fixture
  f.state.sm.model.daylightRoadExponent = value
  assert f.exponent(f.state, Stream.VISION_STREAM_ROAD, f.camera_eof) == 0.0
  assert f.exponent(f.state, Stream.VISION_STREAM_WIDE_ROAD, f.camera_eof) == 0.75


def test_float32_lower_endpoint_and_older_publisher(fixture):
  f = fixture
  f.state.sm.model.daylightRoadExponent = 0.019999999552965164
  assert f.exponent(f.state, Stream.VISION_STREAM_ROAD, f.camera_eof) == f.state.sm.model.daylightRoadExponent
  del f.state.sm.model.daylightRoadExponent
  assert f.exponent(f.state, Stream.VISION_STREAM_ROAD, f.camera_eof) == 0.0


@pytest.mark.parametrize("which", ["stale message", "stale inference frame", "stale displayed frame",
                                   "future frame", "invalid model", "previous session", "offroad", "preview switched off"])
def test_stale_or_invalid_frame_uses_original_shader(fixture, which):
  f = fixture
  stream = Stream.VISION_STREAM_ROAD
  assert f.shader.begin(f.state, stream, f.camera_eof)
  assert f.shader.begin(f.state, stream, f.camera_eof)
  assert f.starts == [1, 1]
  assert f.updates == [(4, 0.5)]
  match which:
    case "stale message":
      f.state.sm.recv_time["modelV2"] = 9.74
    case "stale inference frame":
      f.state.sm.model.timestampEof = 19_749_999_999
    case "stale displayed frame":
      f.camera_eof = 19_749_999_999
    case "future frame":
      f.camera_eof = 20_000_000_001
    case "invalid model":
      f.state.sm.valid["modelV2"] = False
    case "previous session":
      f.state.started_frame = f.state.sm.recv_frame["modelV2"]
    case "offroad":
      f.state.started = False
    case "preview switched off":
      f.state.show_model_daylight75 = False
  assert not f.shader.begin(f.state, stream, f.camera_eof)
  assert not f.shader.begin(f.state, stream, f.camera_eof)
  assert f.starts == [1, 1]
  assert f.updates == [(4, 0.5)]

def test_shader_compiles_only_during_initialization_and_closes_once(fixture):
  f = fixture
  assert f.loaded == [("vertex", "#version 300 es\n#define DAYLIGHT75_ENABLED\nfragment")]
  f.shader.close()
  f.shader.close()
  assert f.unloaded == [1]


def test_shader_falls_back_if_the_active_uniform_is_unavailable(fixture):
  f = fixture
  f.rl.get_shader_location = lambda *args: -1
  shader = f.init_shader("vertex", "#version 300 es\nfragment")
  assert not shader.begin(f.state, Stream.VISION_STREAM_ROAD, f.camera_eof)
  assert not f.starts and not f.updates
