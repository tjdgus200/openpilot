"""Use the current model's Y75 curve in the onroad camera draw."""
import time

import pyray as rl

from msgq.visionipc import VisionStreamType


_Y75_MAX_AGE = 0.25
_CAMERA_CLOCK = getattr(time, "CLOCK_BOOTTIME", None)


def daylight75_exponent(ui_state, stream_type: VisionStreamType, camera_eof_ns: int) -> float:
  if not ui_state.show_model_daylight75 or not ui_state.started or not camera_eof_ns:
    return 0.0
  if stream_type == VisionStreamType.VISION_STREAM_ROAD:
    field = "daylightRoadExponent"
  elif stream_type == VisionStreamType.VISION_STREAM_WIDE_ROAD:
    field = "daylightWideRoadExponent"
  else:
    return 0.0

  sm = ui_state.sm
  if not sm.valid["modelV2"] or sm.recv_frame["modelV2"] <= ui_state.started_frame:
    return 0.0
  model = sm["modelV2"]
  exponent = getattr(model, field, 0.0)
  # The Float32 representation of 0.02 is just below the decimal bound.
  if not 0.019999999 <= exponent < 1.0:
    return 0.0

  if not 0.0 <= time.monotonic() - sm.recv_time["modelV2"] <= _Y75_MAX_AGE:
    return 0.0
  # Camera capture timestamps use BOOTTIME on the device. SubMaster receive time
  # uses MONOTONIC, which can differ after a suspend.
  camera_now_ns = time.clock_gettime_ns(_CAMERA_CLOCK) if _CAMERA_CLOCK is not None else time.monotonic_ns()
  max_age_ns = int(_Y75_MAX_AGE * 1e9)
  if (not 0 <= camera_now_ns - camera_eof_ns <= max_age_ns
      or not 0 <= camera_now_ns - model.timestampEof <= max_age_ns):
    return 0.0
  return exponent


class Daylight75Shader:
  def __init__(self, vertex_source: str, fragment_source: str):
    version, body = fragment_source.lstrip().split("\n", 1)
    self.shader = rl.load_shader_from_memory(vertex_source, version + "\n#define DAYLIGHT75_ENABLED\n" + body)
    self._location = rl.get_shader_location(self.shader, "daylightExponent")
    self._value = rl.ffi.new("float[1]")

  def begin(self, ui_state, stream_type: VisionStreamType, camera_eof_ns: int) -> bool:
    if self._location < 0:
      return False
    exponent = daylight75_exponent(ui_state, stream_type, camera_eof_ns)
    if not exponent:
      return False
    rl.begin_shader_mode(self.shader)
    if self._value[0] != exponent:
      self._value[0] = exponent
      rl.set_shader_value(self.shader, self._location, self._value, rl.ShaderUniformDataType.SHADER_UNIFORM_FLOAT)
    return True

  def close(self) -> None:
    if self.shader.id:
      rl.unload_shader(self.shader)
      self.shader.id = 0


DAYLIGHT75_SHADER = """
#ifdef DAYLIGHT75_ENABLED
uniform float daylightExponent;

// Both paths use the model's 16..160 curve in the received image's luma range.
float daylight75Lift(float normalizedLuma, float range) {
  if (normalizedLuma > 0.0 && normalizedLuma < 1.0) {
    return range * (pow(normalizedLuma, daylightExponent) - normalizedLuma);
  }
  return 0.0;
}
"""

DAYLIGHT75_EGL_SHADER = """
vec3 daylight75RGB(vec3 rgb) {
  // The external sampler supplies RGB. Approximate the original Y using
  // Rec.601 narrow-range coefficients without changing the EGL import.
  const float yRange = 144.0 / 219.0;
  float y = dot(rgb, vec3(0.299, 0.587, 0.114));
  return clamp(rgb + vec3(daylight75Lift(y / yRange, yRange)), 0.0, 1.0);
}
#endif
"""

DAYLIGHT75_Y_SHADER = """
float daylight75Y(float y) {
  const float yBlack = 16.0 / 255.0;
  const float yRange = 144.0 / 255.0;
  return y + daylight75Lift((y - yBlack) / yRange, yRange);
}
#endif
"""
