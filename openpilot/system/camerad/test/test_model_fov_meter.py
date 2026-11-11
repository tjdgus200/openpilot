"""PC-only native sampler/warp equivalence, bounds and capture wiring tests."""
import ctypes
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import get_warp_matrix, wider_model_warp, wide_model_pitch

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope='module')
def native(tmp_path_factory):
  folder = tmp_path_factory.mktemp('fov-meter')
  wrapper = folder / 'wrapper.cc'
  wrapper.write_text('''
#include "system/camerad/cameras/model_fov_meter.h"
#include <thread>
extern "C" int mailbox_check() {
  ModelFovMailbox mailbox;
  std::atomic<bool> done{false};
  std::thread writer([&]() {
    for (uint64_t i = 1; i < 100000; ++i) {
      ModelFovSnapshot value;
      value.ready = i % 2; value.selection = i % 5;
      value.wide_model_down = i % 31;
      value.generation = i; value.valid_until = i + 1; value.sessions = {i * 7, i * 9}; value.mode = i % 4;
      value.status = static_cast<ModelFovStatus>(i % 22); value.assist_mode = (i / 4) % 4;
      value.rpy = {double(i), double(i * 2), double(i * 3)};
      value.lane_mode = i % 4;
      for (int lens=0;lens<2;++lens) { value.boxes[lens].valid = true; value.boxes[lens].valid_until = i+3;
        value.boxes[lens].center_x = float(i); }
      mailbox.publish(value);
    }
    done.store(true);
  });
  bool good = true;
  do {
    ModelFovSnapshot value;
    if (mailbox.read(value) && value.generation) {
      const auto i = value.generation;
      good &= value.ready == bool(i % 2) && value.selection == int(i % 5) && value.valid_until == i + 1 &&
              value.wide_model_down == int(i % 31) &&
              value.sessions[0] == i * 7 && value.sessions[1] == i * 9 && value.mode == int(i % 4) &&
              static_cast<int>(value.status) == int(i % 22) && value.assist_mode == int((i / 4) % 4) &&
              value.rpy[0] == i && value.rpy[1] == i * 2 && value.rpy[2] == i * 3 && value.lane_mode == int(i%4);
      if (value.lane_mode) for (const auto &lane:value.boxes) {
        good &= lane.valid && lane.valid_until == i+3;
        good &= lane.center_x == i;
      }
    }
  } while (!done.load());
  writer.join();
  ModelFovSnapshot last;
  return good && mailbox.read(last) && last.generation == 99999;
}
extern "C" int meter(int width, int height, bool wide, double roll, double pitch, double yaw,
                     int selection, int stride, const uint8_t *pixels, size_t size, int down, float *output) {
  ModelFovMeter meter;
  if (!meter.configure(width, height, wide, {roll, pitch, yaw}, selection, stride, {}, down)) return 0;
  for (int i = 0; i < 8; ++i) output[i] = meter.polygon[i];
  if (!pixels) return 1;
  if (!meter.measure(pixels, size)) return -1;
  output[8] = meter.grey; output[9] = meter.black_fraction; output[10] = meter.white_fraction;
  return 2;
}
extern "C" void stability(const uint64_t *times, const uint64_t *sessions, const uint8_t *valid,
                           size_t count, uint8_t *output) {
  ModelFovStability guard;
  for (size_t i = 0; i < count; ++i) {
    guard.set_session(sessions[i]);
    output[i] = guard.use_fov(valid[i], times[i]) | (guard.locked() << 1);
  }
}
extern "C" void assist(bool wide, int selection, const uint8_t *pixels, size_t bytes,
                      const uint64_t *times, const uint8_t *active, size_t count, float *output) {
  ModelFovMeter meter;
  meter.configure(1928, 1208, wide, {}, selection, 2048);
  for (size_t i = 0; i < count; ++i) {
    const bool valid = meter.measure(pixels, bytes, true);
    output[i * 2] = meter.grey;
    output[i * 2 + 1] = valid ? meter.assisted_grey(active[i], times[i]) : -1;
  }
}
''', encoding='utf-8')
  library = folder / 'meter.so'
  result = subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared', '-pthread',
                  '-I', str(ROOT), str(wrapper), '-o', str(library)], capture_output=True)
  assert result.returncode == 0, result.stderr.decode()
  module = ctypes.CDLL(str(library))
  func = module.meter
  func.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_double, ctypes.c_double, ctypes.c_double,
                   ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
  func.restype = ctypes.c_int

  def call(wide=True, rpy=(0., 0., 0.), selection=0, image=None, size=None, dimensions=(1928, 1208), stride=0, down=0):
    output = (ctypes.c_float * 11)()
    status = func(*dimensions, wide, *rpy, selection, stride or (image.strides[0] if image is not None else 0),
                  image.ctypes.data if image is not None else None,
                  image.nbytes if size is None and image is not None else size or 0, down, output)
    return status, np.array(output)
  call.mailbox_check = module.mailbox_check
  module.stability.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_size_t, ctypes.c_void_p]
  module.stability.restype = None

  def stability(events):
    times = np.array([round(t * 1e9) for t, _, _ in events], dtype=np.uint64)
    sessions = np.array([s for _, s, _ in events], dtype=np.uint64)
    valid = np.array([v for _, _, v in events], dtype=np.uint8)
    output = np.zeros(len(events), dtype=np.uint8)
    module.stability(times.ctypes.data, sessions.ctypes.data, valid.ctypes.data, len(events), output.ctypes.data)
    return output
  call.stability = stability
  module.assist.argtypes = [ctypes.c_bool, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
  module.assist.restype = None

  def assist(image, wide=True, selection=0, times=None, active=None, size=None):
    times = np.array(times if times is not None else np.arange(1., 11., .05), dtype=float)
    stamps = np.rint(times * 1e9).astype(np.uint64)
    active = np.array(active if active is not None else np.ones(len(times)), dtype=np.uint8)
    output = np.zeros((len(times), 2), dtype=np.float32)
    module.assist(wide, selection, image.ctypes.data, image.nbytes if size is None else size,
                  stamps.ctypes.data, active.ctypes.data, len(times), output.ctypes.data)
    return output
  call.assist = assist
  return call


def warp_points(wide, rpy, selection, points):
  camera = DEVICE_CAMERAS['tizi', 'ox03c10']
  warp = get_warp_matrix(np.array(rpy), (camera.ecam if wide else camera.fcam).intrinsics, wide)
  if wide and selection:
    warp = wider_model_warp(warp, (65, 70, 75, 80)[selection - 1])
  result = warp @ np.vstack((np.asarray(points).T, np.ones(len(points))))
  return (result[:2] / result[2]).T


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('selection', range(5))
@pytest.mark.parametrize('rpy', [(0., 0., 0.), (.005, -.005, .005), (.02, -.03, .04), (-.03, .04, -.02)])
def test_native_footprint_matches_actual_model_warp(native, wide, selection, rpy):
  status, output = native(wide, rpy, selection)
  points = [(511 * (col + .5) / 64, 255 * (row + .5) / 36) for row in range(36) for col in range(64)]
  source = warp_points(wide, rpy, selection, points)
  valid = ((source >= 0) & (source < [1928, 1208])).all(axis=1).sum()
  if valid * 10 < 2304 * 9:
    assert status == 0
    return
  assert status == 1
  expected = warp_points(wide, rpy, selection, [(0, 0), (511, 0), (511, 255), (0, 255)])
  np.testing.assert_allclose(output[:8].reshape(4, 2), expected, atol=.001)


@pytest.mark.parametrize('selection', range(5))
def test_histogram_uses_only_cached_model_samples(native, selection):
  image = np.random.default_rng(23).integers(0, 256, size=(1208, 1928), dtype=np.uint8)
  rpy = (.02, -.03, .04)
  points = [(511 * (col + .5) / 64, 255 * (row + .5) / 36) for row in range(36) for col in range(64)]
  source = warp_points(True, rpy, selection, points).astype(int)
  values = image[source[:, 1], source[:, 0]]
  status, output = native(True, rpy, selection, image)
  assert status == 2
  # Match the existing descending histogram's upper median for an even count.
  assert output[8] == np.sort(values)[len(values) // 2] / 256
  np.testing.assert_allclose(output[9:], [(values <= 5).mean(), (values >= 250).mean()], atol=1e-7)
  outside = np.full_like(image, 255)
  outside[source[:, 1], source[:, 0]] = values
  np.testing.assert_equal(native(True, rpy, selection, outside)[1][8:], output[8:])


@pytest.mark.parametrize('kwargs', [{'dimensions': (1344, 760)}, {'selection': 5}, {'selection': -1},
                                    {'rpy': (np.nan, 0., 0.)}, {'wide': False, 'rpy': (0., -.1, 0.)}, {'rpy': (1., 0., 0.)}])
def test_invalid_or_mostly_clipped_geometry_falls_back(native, kwargs):
  assert native(**kwargs)[0] == 0


def test_short_buffer_and_black_frame_fall_back(native):
  image = np.zeros((1208, 1928), dtype=np.uint8)
  assert native(image=image, size=8)[0] == -1
  assert native(image=image)[0] == -1
  image.fill(255)
  assert native(image=image)[0] == 2


def test_real_nv12_row_padding_is_not_sampled_as_image(native):
  logical = np.random.default_rng(7).integers(10, 240, size=(1208, 1928), dtype=np.uint8)
  padded = np.full((1208, 2048), 255, dtype=np.uint8)
  padded[:, :1928] = logical
  status, output = native(image=padded)
  assert status == 2
  np.testing.assert_equal(output[8:], native(image=logical)[1][8:])
  assert native(image=padded, size=1928 * 1208)[0] == -1
  assert native(stride=1)[0] == 0


def assisted_scene(wide, selection, kind):
  """Synthetic luminance at independently projected model samples, padded NV12 Y."""
  row, col = np.indices((36, 64))
  u, v = 511 * (col + .5) / 64, 255 * (row + .5) / 36
  values = np.full((36, 64), 100, dtype=np.uint8)
  if kind == 'dark_road':
    values[row < 19] = 180
    values[row >= 19] = 30
  elif kind == 'black_sky':
    values[row < 12] = 0
  elif kind == 'road_shadow':
    values[(row >= 24) & (col < 22)] = 0
  elif kind == 'glare':
    values[(row < 12) & (col >= 22) & (col < 43)] = 255
    values[(row >= 24) & (col < 22)] = 0
  elif kind == 'white':
    values.fill(255)
  elif kind == 'black':
    values.fill(0)
  source = warp_points(wide, (0., 0., 0.), selection, np.column_stack((u.ravel(), v.ravel()))).astype(int)
  assert ((source >= 0) & (source < [1928, 1208])).all()
  assert len(np.unique(source, axis=0)) == 2304
  image = np.full((1208, 2048), 233, dtype=np.uint8)
  image[source[:, 1], source[:, 0]] = values.ravel()
  return image


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('selection', range(5))
@pytest.mark.parametrize('kind,expected_bias', [
  ('uniform', 0.), ('dark_road', .25), ('black_sky', 0.),
  ('road_shadow', .125), ('glare', -.125), ('white', None),
])
def test_assist_scene_response_is_bounded_smooth_and_directional(native, wide, selection, kind, expected_bias):
  image = assisted_scene(wide, selection, kind)
  output = native.assist(image, wide, selection)
  assert np.isfinite(output).all() and ((output > 0) & (output <= 1)).all()
  # Uniform metering remains unchanged; corrected grey is only the AE input.
  baseline = native(wide, selection=selection, image=image)[1][8]
  np.testing.assert_array_equal(output[:, 0], np.full(len(output), baseline))
  assert output[0, 1] == baseline  # No entry step.
  bias = np.log2(output[:, 0] / output[:, 1])
  assert np.max(np.abs(bias)) <= .375001
  assert np.max(np.abs(np.diff(bias))) <= .020001  # 0.4 EV/s at 20 Hz.
  if expected_bias is not None:
    assert bias[-1] == pytest.approx(expected_bias, abs=.001)
  else:
    assert output[-1, 1] == 1.  # White frame never produces invalid grey > 1.


def test_assist_resets_on_disable_gap_and_backward_clock(native):
  image = assisted_scene(True, 4, 'dark_road')
  times = [1., 1.05, 1.10, 1.15, 1.20, 2., 2.05, 1.9, 1.95]
  active = [1, 1, 0, 1, 1, 1, 1, 1, 1]
  output = native.assist(image, selection=4, times=times, active=active)
  for i in [0, 2, 3, 5, 7]:
    assert output[i, 1] == output[i, 0]
  for i in [1, 4, 6, 8]:
    assert output[i, 1] < output[i, 0]


def test_assisted_invalid_buffers_and_black_frames_are_rejected(native):
  image = assisted_scene(True, 0, 'black')
  assert (native.assist(image)[:, 1] == -1).all()
  image.fill(100)
  assert (native.assist(image, size=1928 * 1208)[:, 1] == -1).all()


def test_latest_snapshot_cannot_mix_calibration_or_fov_generations(native):
  assert native.mailbox_check() == 1


def frames(start, stop, valid=True, session=1):
  # Match capture cadence; use wall time rather than an arbitrary frame count.
  return [(float(t), session, valid) for t in np.arange(start, stop + .001, .05)]


@pytest.mark.parametrize('start', [0., 100., 86400.])
def test_fov_entry_and_recovery_require_two_seconds_of_valid_frames(native, start):
  events = frames(start, start + 2.) + [(start + 2.05, 1, False)] + frames(start + 2.1, start + 4.1)
  output = native.stability(events)
  assert not output[:40].any()
  assert output[40] == 1
  assert output[41] == 0  # Not a grace period for a corrupt buffer or expired context.
  assert not output[42:82].any()
  assert output[82] == 1


@pytest.mark.parametrize('gap', [.05, .1, .2, .25])
def test_valid_camera_cadences_use_elapsed_time_not_frame_count(native, gap):
  events = [(float(t), 1, True) for t in np.arange(0., 2.001, gap)]
  output = native.stability(events)
  assert not output[:-1].any() and output[-1] == 1


@pytest.mark.parametrize('invalid_at', [.05, 1., 1.95])
def test_failure_while_warming_restarts_dwell_without_counting_a_switch(native, invalid_at):
  events = frames(0., invalid_at - .05) + [(invalid_at, 1, False)] + frames(invalid_at + .05, invalid_at + 2.05)
  output = native.stability(events)
  assert not output[:-1].any() and output[-1] == 1


def test_missing_capture_observations_cannot_instantly_complete_warmup(native):
  events = frames(0., 1.) + frames(3., 5.)
  output = native.stability(events)
  assert not output[:-1].any() and output[-1] == 1


def test_three_actual_fallbacks_in_thirty_seconds_latch_stock_until_observed_option_edge(native):
  events = []
  for start in [0., 3., 6.]:
    events += frames(start, start + 2.) + [(start + 2.05, 1, False)]
  events += frames(8.1, 70.)  # Neither elapsed time nor good input silently rearms it.
  output = native.stability(events)
  assert np.count_nonzero(output == 1) == 3
  assert (output[125:] == 2).all()
  events += [(70.05, 2, False)] + frames(70.1, 72.1, session=3)
  output = native.stability(events)
  assert not output[-41:-1].any() and output[-1] == 1


def test_continuous_invalid_frames_count_only_one_actual_fallback(native):
  events = frames(0., 2.) + frames(2.05, 20., False) + frames(20.05, 22.05)
  output = native.stability(events)
  assert output[40] == 1 and output[-1] == 1
  assert not (output & 2).any()


def test_three_sparse_fallbacks_outside_rolling_window_do_not_latch(native):
  events = []
  for start in [0., 20., 40., 60.]:
    events += frames(start, start + 2.) + [(start + 2.05, 1, False)]
  output = native.stability(events)
  assert np.count_nonzero(output == 1) == 4
  assert not (output & 2).any()


@pytest.mark.parametrize('last_start,locked', [(30., True), (30.05, False)])
def test_failure_window_boundary_is_rolling_and_inclusive(native, last_start, locked):
  events = []
  for start in [0., 15., last_start]:
    events += frames(start, start + 2.) + [(start + 2.05, 1, False)]
  assert native.stability(events)[-1] == (2 if locked else 0)


def test_option_change_stops_immediately_and_requires_new_entry_dwell(native):
  events = frames(0., 2.) + [(2.05, 2, False)] + frames(2.1, 4.1, session=3)
  output = native.stability(events)
  assert output[40] == 1 and not output[41:-1].any() and output[-1] == 1


def test_backward_clock_fails_closed_until_new_option_session(native):
  events = frames(10., 12.) + [(11., 1, True)] + frames(12.05, 15.)
  output = native.stability(events)
  assert output[40] == 1 and (output[41:] == 2).all()


def test_long_valid_episode_does_not_expire_or_add_a_new_switch(native):
  output = native.stability(frames(0., 120.))
  assert not output[:40].any() and (output[40:] == 1).all()


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('selection', range(5))
@pytest.mark.parametrize('down', [0, 1, 10, 30])
def test_actual_down_native_meter_matches_model_warp_and_road_is_unchanged(native, wide, selection, down):
  rpy = np.array([.005, -.005, .005], dtype=float)
  lens = DEVICE_CAMERAS['tizi', 'ox03c10'].ecam if wide else DEVICE_CAMERAS['tizi', 'ox03c10'].fcam
  adjusted = wide_model_pitch(rpy, down) if wide else rpy
  warp = get_warp_matrix(adjusted, lens.intrinsics, wide)
  if wide and selection:
    warp = wider_model_warp(warp, (65, 70, 75, 80)[selection - 1])
  rays = warp @ np.array([[0, 511, 511, 0], [0, 0, 255, 255], [1, 1, 1, 1]])
  expected = (rays[:2] / rays[2]).T.reshape(-1)
  status, values = native(wide=wide, rpy=rpy, selection=selection, down=down)
  assert status == 1
  np.testing.assert_allclose(values[:8], expected, atol=.0003)
  if not wide:
    np.testing.assert_array_equal(values, native(wide=False, rpy=rpy, selection=selection, down=0)[1])


@pytest.mark.parametrize('down', [-1, 31, 999])
def test_native_meter_rejects_bad_actual_down(native, down):
  assert native(down=down)[0] == 0


def test_camera_owner_keeps_ipc_and_disk_reads_out_of_frame_delivery():
  source = (ROOT / 'system/camerad/cameras/camera_qcom2.cc').read_text(encoding='utf-8')
  delivery = source.split('void CameraState::sendState(', 1)[1].split('void camerad_thread()', 1)[0]
  frame_loop = source.split('ModelFovWorker fov_worker(', 1)[1]
  assert 'params.get' not in delivery and 'sm->update' not in delivery
  assert 'fov_worker.latest()' in frame_loop and 'context.update(' not in frame_loop
  assert 'SCHED_OTHER' in source and 'setpriority(PRIO_PROCESS, 0, 10)' in source
  assert 'fov_active ? metered_grey :' in delivery and 'calculate_exposure_value(' in delivery
  assert 'now < context.valid_until' in delivery and '!fov_stability.locked()' in delivery
  assert 'fov_stability.set_session(context.sessions[wide])' in delivery
  assert 'context.camera_enabled(wide)' in delivery
  assert 'fov_stability.use_fov(fov_valid, now)' in delivery


def test_default_off_and_camera_menu_keep_display_separate_from_metering():
  settings = json.loads((ROOT / 'selfdrive/carrot_settings.json').read_text(encoding='utf-8'))
  params = {item['name']: item for item in settings['params']}
  assert params['Pv5ModelFovExposure']['default'] == params['Pv5CameraFovPreview']['default'] == 0
  metering = params['Pv5ModelFovExposure']
  assert metering['control'] == 'select' and metering['max'] == 3
  assert metering['options']['ko'] == ['끔 (기존 자동 노출)', '일반각 + 광각', '일반각만', '광각만']
  assert all(len(options) == 4 for options in metering['options'].values())
  category = next(c for c in settings['menu'] if c['id'] == 'PV5')
  camera = next(g for g in category['groups'] if g['id'] == 'PV5_CAMERA')
  assert camera['groups'][0]['params'] == ['Pv5WideModelFov', 'Pv5WideModelDown', 'Pv5ModelFovExposure', 'Pv5RoadExposureAssist', 'Pv5WideExposureAssist', 'Pv5RoadLaneExposure', 'Pv5WideLaneExposure']
  assert camera['groups'][1]['params'] == ['Pv5CameraFovPreview', 'Pv5FovPreviewPitch']
  keys = (ROOT / 'common/params_keys.h').read_text(encoding='utf-8')
  for key in ('Pv5RoadExposureAssist', 'Pv5WideExposureAssist', 'Pv5RoadLaneExposure', 'Pv5WideLaneExposure'):
    assert params[key]['default'] == 0 and params[key]['control'] == 'toggle'
    assert key in keys


def test_actual_frame_selection_exits_on_fault_and_latches_repeated_fallbacks(tmp_path):
  """Real production selection block, guard and pixel sampler; mocked sensor/AE boundary."""
  source = (ROOT / 'system/camerad/cameras/camera_qcom2.cc').read_text(encoding='utf-8')
  selection = 'const bool front' + source.split('const bool front', 1)[1].split('set_camera_exposure(grey);', 1)[0]
  selection += 'set_camera_exposure(grey);'
  fixture = r'''
#include "system/camerad/cameras/model_fov_meter.h"
#include <cassert>
#include <vector>
uint64_t clock_now = 0;
uint64_t nanos_since_boot() { return clock_now; }
constexpr int VISION_STREAM_ROAD = 0, VISION_STREAM_WIDE_ROAD = 1, VISION_STREAM_DRIVER = 2;
namespace cereal { struct FrameData { enum class ImageSensor { AR0231, OX03C10, OTHER }; }; }
struct Sensor { cereal::FrameData::ImageSensor image_sensor = cereal::FrameData::ImageSensor::OX03C10; };
struct Yuv { uint8_t *y = nullptr; size_t len = 0; int stride = 1928; };
struct Buffer { int out_img_width = 1928, out_img_height = 1208; Yuv *cur_yuv_buf = nullptr; };
struct Camera { struct { int stream_type = VISION_STREAM_WIDE_ROAD; } cc; Sensor *sensor; Buffer buf; };
struct Polygon { std::array<float,8> *points; void set(size_t i, float value) { (*points)[i]=value; } };
struct Frame {
  bool active = false;
  uint8_t status = 0;
  uint16_t warmup_ms = 0;
  std::array<float,8> points = {};
  void setPv5ExposureFovActive(bool value) { active = value; }
  void setPv5ExposureFovStatus(uint8_t value) { status = value; }
  void setPv5ExposureFovWarmupMs(uint16_t value) { warmup_ms = value; }
  Polygon initPv5ExposureFovPolygon(size_t) { return {&points}; }
  void setPv5ExposureBlackFraction(float) {}
  void setPv5ExposureWhiteFraction(float) {}
};
float calculate_exposure_value(Buffer *, ModelFovRegion, int, int) { return .25f; }
struct Capture {
  Camera camera;
  Frame framed;
  ModelFovMeter fov_meter;
  ModelFovStability fov_stability;
  uint64_t fov_generation = UINT64_MAX;
  ModelFovRegion fov_region;
  bool fov_configured = false;
  ModelFovRegion ae_xywh{97,160,1734,986};
  float selected = 0;
  void set_camera_exposure(float grey) { selected = grey; }
  void step(const ModelFovSnapshot &context) {
    framed.active = false;  // A fresh FrameData message is built for every capture.
'''
  scenarios = r'''
  }
};
int main() {
  std::vector<uint8_t> pixels(1928 * 1208, 128);
  // Actual frame selection for all four choices and all three capture streams.
  // Driver exposure always remains stock, including legacy mode1/both.
  for (int assist = 0; assist < 4; ++assist) for (int mode = 0; mode < 4; ++mode) for (int lane = 0; lane < 4; ++lane) for (int stream = 0; stream < 3; ++stream) {
    Sensor sensor;
    Yuv yuv{pixels.data(), pixels.size(), 1928};
    Capture capture{{{stream}, &sensor, {1928, 1208, &yuv}}};
    ModelFovSnapshot context;
    context.ready = mode != 0 || assist != 0 || lane != 0; context.mode = mode; context.assist_mode = assist; context.lane_mode = lane; context.sessions = {1, 1};
    context.status = context.ready ? ModelFovStatus::ACTIVE : ModelFovStatus::DISABLED;
    const bool selected = stream < 2 && context.camera_enabled(stream == 1);
    clock_now = 0;
    for (int i = 0; i <= 50; ++i) {
      context.valid_until = clock_now + 1000000000ULL;
      for(auto &box:context.boxes) { box.valid=true;box.center_x=1100;box.valid_until=context.valid_until; }
      capture.step(context);
      const bool active = selected && i >= 40;
      assert(capture.framed.active == active);
      assert(capture.selected == (active && context.camera_fov_base(stream==1) ? .5f : .25f));
      if(active&&context.camera_lane(stream==1)) {
        for(int corner=0;corner<4;++corner)assert(capture.framed.points[corner*2]>=0&&capture.framed.points[corner*2]<1928);
        const float expected_width=context.camera_fov_base(stream==1) ? capture.fov_meter.polygon[2]-capture.fov_meter.polygon[0] : capture.ae_xywh.w-1;
        assert(std::abs(capture.framed.points[2]-capture.framed.points[0]-expected_width)<.001);
      }
      assert(capture.framed.status == static_cast<uint8_t>(active ? ModelFovStatus::ACTIVE :
        selected ? ModelFovStatus::WARMING : ModelFovStatus::DISABLED));
      assert(capture.framed.warmup_ms == (selected && i < 40 ? i * 50 : 0));
      clock_now += 50000000ULL;
    }
  }
  const ModelFovStatus fault_status[] = {ModelFovStatus::INPUT_UNAVAILABLE, ModelFovStatus::INPUT_EXPIRED,
    ModelFovStatus::INVALID_BUFFER, ModelFovStatus::INVALID_BUFFER, ModelFovStatus::UNSUPPORTED,
    ModelFovStatus::CALIBRATION_INVALID, ModelFovStatus::INVALID_BRIGHTNESS, ModelFovStatus::REGION_CLIPPED};
  for (int assist = 0; assist < 2; ++assist) for (int fault = 0; fault < 8; ++fault) {
    Sensor sensor;
    Yuv yuv{pixels.data(), pixels.size(), 1928};
    Capture capture{{{}, &sensor, {1928, 1208, &yuv}}};
    ModelFovSnapshot context;
    context.ready = true; context.mode = 1; context.assist_mode = assist ? 3 : 0; context.sessions = {1, 1};
    context.status = ModelFovStatus::ACTIVE;
    clock_now = 10000000000ULL;
    auto step = [&](bool want_fov, bool refresh = true) {
      if (refresh) context.valid_until = clock_now + 1000000000ULL;
      capture.step(context);
      assert(capture.framed.active == want_fov);
      assert(capture.selected == (want_fov ? .5f : .25f));
      clock_now += 50000000ULL;
    };
    auto warm = [&]() {
      for (int i = 0; i < 40; ++i) { ++context.generation; step(false); }
      step(true);  // Normal calibration generations do not restart dwell.
    };
    for (int episode = 0; episode < 3; ++episode) {
      warm();
      if (fault == 0) { context.ready = false; context.status = ModelFovStatus::INPUT_UNAVAILABLE; }
      if (fault == 1) context.valid_until = clock_now;  // Expiry at the frame, after worker read.
      if (fault == 2) yuv.len = 8;
      if (fault == 3) yuv.y = nullptr;
      if (fault == 4) sensor.image_sensor = cereal::FrameData::ImageSensor::OTHER;
      if (fault == 5) { context.rpy[1] = .8; ++context.generation; }
      if (fault == 6) std::fill(pixels.begin(), pixels.end(), 0);
      if (fault == 7) { capture.camera.cc.stream_type = VISION_STREAM_ROAD; context.rpy[1] = -.1; ++context.generation; }
      step(false, fault != 1);  // Each fault selects stock on this very frame.
      assert(capture.framed.status == static_cast<uint8_t>(episode == 2 ? ModelFovStatus::REPEATED_FALLBACK : fault_status[fault]));
      assert(capture.framed.warmup_ms == 0);
      context.ready = true; context.status = ModelFovStatus::ACTIVE; context.rpy = {}; ++context.generation;
      yuv.len = pixels.size(); yuv.y = pixels.data();
      sensor.image_sensor = cereal::FrameData::ImageSensor::OX03C10;
      std::fill(pixels.begin(), pixels.end(), 128);
    }
    assert(capture.fov_stability.locked());
    for (int i = 0; i < 100; ++i) { ++context.generation; step(false); }
    context.sessions = {2, 2}; context.ready = false; step(false);
    assert(!capture.fov_stability.locked());
    context.sessions = {3, 3}; context.ready = true; warm();
  }
  // A wide-camera fault/rearm cannot interrupt road FOV metering, or vice versa.
  Sensor sensor;
  Yuv road_yuv{pixels.data(), pixels.size(), 1928}, wide_yuv = road_yuv;
  Capture road{{{VISION_STREAM_ROAD}, &sensor, {1928, 1208, &road_yuv}}};
  Capture wide{{{VISION_STREAM_WIDE_ROAD}, &sensor, {1928, 1208, &wide_yuv}}};
  ModelFovSnapshot pair;
  pair.ready = true; pair.mode = 1; pair.sessions = {1, 1};
  pair.status = ModelFovStatus::ACTIVE;
  clock_now = 0;
  auto step_pair = [&](bool road_active, bool wide_active) {
    pair.valid_until = clock_now + 1000000000ULL;
    road.step(pair); wide.step(pair);
    assert(road.framed.active == road_active && wide.framed.active == wide_active);
    clock_now += 50000000ULL;
  };
  for (int episode = 0; episode < 3; ++episode) {
    for (int i = 0; i < 40; ++i) step_pair(episode != 0, false);
    step_pair(true, true);
    wide_yuv.len = 8; step_pair(true, false); wide_yuv.len = pixels.size();
  }
  assert(wide.fov_stability.locked() && !road.fov_stability.locked());
  pair.mode = 2; ++pair.sessions[1]; step_pair(true, false);
  assert(!wide.fov_stability.locked());
  pair.mode = 1; ++pair.sessions[1];
  for (int i = 0; i < 40; ++i) step_pair(true, false);
  step_pair(true, true);
  pair.mode = 3; ++pair.sessions[0]; step_pair(false, true);
  pair.mode = 1; ++pair.sessions[0];
  for (int i = 0; i < 40; ++i) step_pair(false, true);
  step_pair(true, true);
  clock_now -= 200000000ULL;
  step_pair(false, false);
  assert(road.framed.status == static_cast<uint8_t>(ModelFovStatus::CLOCK_INVALID));
  assert(wide.framed.status == static_cast<uint8_t>(ModelFovStatus::CLOCK_INVALID));
  ModelFovSnapshot unavailable;
  Capture startup{{{}, &sensor, {1928, 1208, &wide_yuv}}};
  startup.step(unavailable);
  assert(!startup.framed.active && startup.framed.status == static_cast<uint8_t>(ModelFovStatus::WORKER_UNAVAILABLE));
  unavailable.status = ModelFovStatus::WORKER_ERROR;
  startup.step(unavailable);
  assert(startup.framed.status == static_cast<uint8_t>(ModelFovStatus::WORKER_ERROR));
}
'''
  wrapper = tmp_path / 'frame.cc'
  wrapper.write_text(fixture + selection + scenarios, encoding='utf-8')
  executable = tmp_path / 'frame'
  result = subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-error=missing-field-initializers',
                  '-I', str(ROOT), str(wrapper), '-o', str(executable)], capture_output=True)
  assert result.returncode == 0, result.stderr.decode()
  result = subprocess.run([str(executable)], capture_output=True, timeout=10)
  assert result.returncode == 0, result.stderr.decode()


def test_camera_context_faults_and_live_switches_use_the_actual_cpp_class(tmp_path):
  """Exercise production decisions with deterministic IPC/Params boundaries.

  This is not a physical IPC/sensor test. Schema and complete translation-unit
  compilation are separate checks; these fakes supply the received values.
  """
  source = (ROOT / 'system/camerad/cameras/camera_qcom2.cc').read_text(encoding='utf-8')
  context = 'class ModelFovContext' + source.split('class ModelFovContext', 1)[1].split('// Disk reads', 1)[0]
  fixture = r'''
#include "system/camerad/cameras/model_fov_meter.h"
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
uint64_t clock_now = 10000000000ULL;
uint64_t nanos_since_boot() { return clock_now; }
bool env_ctrl_exp_from_params = false;
std::map<std::string, std::string> values;
struct Params {
  std::string get(const std::string &key) { return values[key]; }
  bool getBool(const std::string &key) { return get(key) == "1"; }
};
namespace cereal {
struct InitData { enum class DeviceType { TICI, TIZI, MICI }; };
struct LiveCalibrationData { enum class Status { CALIBRATED, UNCALIBRATED }; };
struct CarParams {};
enum class LaneChangeState { OFF, STARTING };
}
bool not_car = false, pv5 = true, messages_valid = true, new_calibration = true;
struct Car {
  bool getNotCar() const { return not_car; }
  std::string getCarFingerprint() const { return pv5 ? "KIA_PV5" : "OTHER"; }
};
namespace kj {
struct Exception : std::exception {};
template<class T> T *arrayPtr(T *p, size_t) { return p; }
}
namespace capnp {
using word = uint64_t;
struct FlatArrayMessageReader {
  explicit FlatArrayMessageReader(word *) {}
  template<class T> Car getRoot() { return {}; }
};
}
uint64_t cal_time = clock_now, dev_time = clock_now;
std::vector<double> angles = {0, 0, 0};
cereal::InitData::DeviceType device = cereal::InitData::DeviceType::TIZI;
cereal::LiveCalibrationData::Status calibrated = cereal::LiveCalibrationData::Status::CALIBRATED;
struct Calibration {
  auto getCalStatus() const { return calibrated; }
  auto getRpyCalib() const { return angles; }
};
struct Device { auto getDeviceType() const { return device; } };
bool model_valid = true;
uint64_t model_age = 0, model_event_age = 0;
cereal::LaneChangeState lane_change = cereal::LaneChangeState::OFF;
struct Lane {
  int side;
  std::vector<float> getX() const { std::vector<float> x; for (int i=0;i<33;++i) x.push_back(i*2); return x; }
  std::vector<float> getY() const { return std::vector<float>(33, side == 1 ? -1.8f : 1.8f); }
  std::vector<float> getZ() const { return std::vector<float>(33, 1.4f); }
};
struct Meta { auto getLaneChangeState() const { return lane_change; } };
struct Model {
  uint64_t getTimestampEof() const { return clock_now - model_age; }
  Meta getMeta() const { return {}; }
  auto getLaneLines() const { return std::vector<Lane>{{0},{1},{2},{3}}; }
  auto getLaneLineProbs() const { return std::vector<float>(4,.99f); }
  auto getLaneLineStds() const { return std::vector<float>(4,.1f); }
};
struct Event {
  bool cal = false;
  bool model = false;
  uint64_t getLogMonoTime() const { return model ? clock_now - model_event_age : cal ? cal_time : dev_time; }
  Calibration getLiveCalibration() const { return {}; }
  Device getDeviceState() const { return {}; }
  Model getModelV2() const { return {}; }
};
struct SubMaster {
  explicit SubMaster(std::vector<const char *>) {}
  void update(int timeout) { assert(timeout == 0); }
  bool allAliveAndValid(std::vector<const char *> names) { return messages_valid && (std::string(names[0]) != "modelV2" || model_valid); }
  bool updated(const char *) { return new_calibration; }
  Event &operator[](const char *key) { static Event cal{true}, dev{false}, model{false,true}; return std::string(key) == "modelV2" ? model : std::string(key) == "liveCalibration" ? cal : dev; }
};
'''
  scenarios = r'''
int main() {
  for (int assist = 0; assist < 2; ++assist) for (int scenario = 0; scenario < 20; ++scenario) {
    clock_now = 10000000000ULL; cal_time = dev_time = clock_now;
    values = {{"Pv5ModelFovExposure", "1"}, {"UseWideCamera", "1"}, {"Pv5WideModelFov", "4"},
              {"CameraYawTrimDeg", "0"}, {"CarParams", "12345678"}};
    not_car = false; pv5 = true; messages_valid = true; env_ctrl_exp_from_params = false; new_calibration = true;
    angles = {0, 0, 0}; device = cereal::InitData::DeviceType::TIZI;
    calibrated = cereal::LiveCalibrationData::Status::CALIBRATED;
    if (assist) { values["Pv5ModelFovExposure"] = "0"; values["Pv5RoadExposureAssist"] = "1"; values["Pv5WideExposureAssist"] = "1"; }
    ModelFovContext context;
    if (scenario == 1) { values["Pv5RoadExposureAssist"] = "0"; values["Pv5WideExposureAssist"] = "0"; }
    if (scenario == 1) values["Pv5ModelFovExposure"] = "0";
    if (scenario == 2) values["UseWideCamera"] = "0";
    if (scenario == 3) pv5 = false;
    if (scenario == 4) not_car = true;
    if (scenario == 5) device = cereal::InitData::DeviceType::MICI;
    if (scenario == 6) calibrated = cereal::LiveCalibrationData::Status::UNCALIBRATED;
    if (scenario == 7) cal_time -= 1000000001ULL;
    if (scenario == 8) dev_time -= 2500000001ULL;
    if (scenario == 9) cal_time += 1;
    if (scenario == 10) messages_valid = false;
    if (scenario == 11) angles[1] = std::nan("");
    if (scenario == 12) env_ctrl_exp_from_params = true;
    if (scenario == 13) values["CarParams"] = "bad";
    if (scenario == 14) values["CameraYawTrimDeg"] = "100";
    if (scenario == 18) values["UseWideCamera"] = "";
    context.update(scenario != 15);
    const ModelFovStatus expected[] = {ModelFovStatus::ACTIVE, ModelFovStatus::DISABLED, ModelFovStatus::UNSUPPORTED,
      ModelFovStatus::VEHICLE_UNAVAILABLE, ModelFovStatus::VEHICLE_UNAVAILABLE, ModelFovStatus::UNSUPPORTED,
      ModelFovStatus::CALIBRATION_PENDING, ModelFovStatus::CALIBRATION_EXPIRED, ModelFovStatus::DEVICE_EXPIRED,
      ModelFovStatus::TIMESTAMP_INVALID, ModelFovStatus::INPUT_UNAVAILABLE, ModelFovStatus::CALIBRATION_INVALID,
      ModelFovStatus::MANUAL_EXPOSURE, ModelFovStatus::VEHICLE_UNAVAILABLE, ModelFovStatus::YAW_CHANGED,
      ModelFovStatus::UNSUPPORTED, ModelFovStatus::ACTIVE, ModelFovStatus::ACTIVE, ModelFovStatus::ACTIVE, ModelFovStatus::ACTIVE};
    assert(context.status == expected[scenario]);
    if (scenario == 0 || scenario >= 16) {
      assert(context.ready && context.selection == 4);
      if (scenario == 16) {
        values["Pv5ModelFovExposure"] = "0"; values["Pv5RoadExposureAssist"] = "0"; values["Pv5WideExposureAssist"] = "0"; clock_now += 250000000ULL;
        context.update(true); assert(!context.ready && context.status == ModelFovStatus::DISABLED);
      } else if (scenario == 17) {
        clock_now += 1100000000ULL; context.update(true); assert(!context.ready && context.status == ModelFovStatus::CALIBRATION_EXPIRED);
      } else if (scenario == 19) {
        values["CameraYawTrimDeg"] = "100"; clock_now += 250000000ULL;
        context.update(true); assert(!context.ready && context.status == ModelFovStatus::YAW_CHANGED);
        values["CameraYawTrimDeg"] = "0"; clock_now += 250000000ULL;
        context.update(true); assert(!context.ready && context.status == ModelFovStatus::YAW_CHANGED);
      }
    } else assert(!context.ready);
  }
  // No new calibration packet is needed for each frame, but holding it must
  // never extend its original source expiry. FOV updates do not reset session.
  clock_now = 20000000000ULL; cal_time = dev_time = clock_now;
  values = {{"Pv5ModelFovExposure", "1"}, {"UseWideCamera", "1"}, {"Pv5WideModelFov", "4"},
            {"CameraYawTrimDeg", "0"}, {"CarParams", "12345678"}};
  not_car = false; pv5 = true; messages_valid = true; env_ctrl_exp_from_params = false; new_calibration = true;
  angles = {0, 0, 0}; device = cereal::InitData::DeviceType::TIZI;
  calibrated = cereal::LiveCalibrationData::Status::CALIBRATED;
  ModelFovContext context;
  context.update(true); assert(context.ready && context.sessions[0] == 1 && context.sessions[1] == 1);
  const auto expiry = context.valid_until;
  new_calibration = false; clock_now += 750000000ULL;
  context.update(true); assert(context.ready && context.valid_until == expiry && context.sessions[0] == 1 && context.sessions[1] == 1);
  values["Pv5WideModelFov"] = "3"; clock_now += 250000000ULL;
  context.update(true); assert(context.ready && context.selection == 3 && context.sessions[0] == 1 && context.sessions[1] == 1);
  clock_now += 1; context.update(true); assert(!context.ready);
  values["Pv5ModelFovExposure"] = "0"; clock_now += 250000000ULL;
  context.update(true); assert(!context.ready && context.sessions[0] == 2 && context.sessions[1] == 2);
  values["Pv5ModelFovExposure"] = "1"; clock_now += 250000000ULL;
  cal_time = dev_time = clock_now; new_calibration = true;
  context.update(true); assert(context.ready && context.sessions[0] == 3 && context.sessions[1] == 3);
  for (const auto &entry : std::vector<std::pair<std::string, int>>{{"10", 10}, {"30", 30}, {"-1", 0}, {"31", 0}, {"bad", 0}, {"", 0}}) {
    values["Pv5WideModelDown"] = entry.first;
    clock_now += 250000000ULL; cal_time = dev_time = clock_now;
    context.update(true);
    assert(context.ready && context.wide_model_down == entry.second);
    assert(context.sessions[0] == 3 && context.sessions[1] == 3 && context.rpy[1] == 0);
  }
  // All sixteen live mode transitions preserve the unaffected camera's session.
  const bool selected[][2] = {{false, false}, {true, true}, {true, false}, {false, true}};
  for (int before = 0; before < 4; ++before) for (int after = 0; after < 4; ++after) {
    values["Pv5ModelFovExposure"] = std::to_string(before);
    ModelFovContext pair;
    pair.update(true);
    assert(pair.ready == (before != 0));
    for (int lens = 0; lens < 2; ++lens) {
      assert(pair.camera_enabled(lens) == selected[before][lens]);
      assert(pair.sessions[lens] == uint64_t(selected[before][lens]));
    }
    values["Pv5ModelFovExposure"] = std::to_string(after);
    clock_now += 250000000ULL; cal_time = dev_time = clock_now;
    pair.update(true);
    assert(pair.ready == (after != 0));
    for (int lens = 0; lens < 2; ++lens) {
      assert(pair.camera_enabled(lens) == selected[after][lens]);
      assert(pair.sessions[lens] == uint64_t(selected[before][lens]) + (selected[before][lens] != selected[after][lens]));
    }
  }
  // All 256 effective method transitions, with independent per-lens rearm.
  for (int before = 0; before < 64; ++before) for (int after = 0; after < 64; ++after) {
    auto select = [&](int setting) {
      values["Pv5ModelFovExposure"] = std::to_string(setting % 4);
      values["Pv5RoadExposureAssist"] = setting & 4 ? "1" : "0";
      values["Pv5WideExposureAssist"] = setting & 8 ? "1" : "0";
      values["Pv5RoadLaneExposure"] = setting & 16 ? "1" : "0";
      values["Pv5WideLaneExposure"] = setting & 32 ? "1" : "0";
      clock_now += 250000000ULL; cal_time = dev_time = clock_now;
    };
    select(before); ModelFovContext pair; pair.update(true);
    const auto sessions = pair.sessions;
    const int methods[] = {pair.camera_signature(false), pair.camera_signature(true)};
    select(after); pair.update(true);
    for (int lens = 0; lens < 2; ++lens) {
      const int expected = (after & (lens ? 8 : 4)) ? 2 : selected[after % 4][lens] ? 1 : 0;
      const bool lane = after & (lens ? 32 : 16);
      assert(pair.camera_method(lens) == (lane ? 3 : expected));
      assert(pair.camera_fov_base(lens) == (expected != 0));
      assert(pair.sessions[lens] == sessions[lens] + ((expected | (lane ? 4 : 0)) != methods[lens]));
    }
  }
  values["Pv5RoadLaneExposure"] = "0"; values["Pv5WideLaneExposure"] = "0";
  values["Pv5RoadExposureAssist"] = "0"; values["Pv5WideExposureAssist"] = "0";
  for (auto invalid : {"", "-1", "4", "11", "true", "nan"}) {
    values["Pv5ModelFovExposure"] = invalid;
    ModelFovContext invalid_context;
    invalid_context.update(true);
    assert(!invalid_context.ready && !invalid_context.camera_enabled(false) && !invalid_context.camera_enabled(true));
  }
  // Optional model loss never disables a valid selected base or changes its session.
  values["Pv5ModelFovExposure"] = "0"; values["Pv5RoadLaneExposure"] = "1"; values["Pv5WideLaneExposure"] = "1";
  cal_time = dev_time = clock_now; ModelFovContext lanes; lanes.update(true);
  assert(lanes.ready && lanes.boxes[0].valid && !lanes.boxes[1].valid);
  clock_now += 500000000ULL; cal_time = dev_time = clock_now; lanes.update(true);
  assert(lanes.ready && lanes.boxes[0].valid && lanes.boxes[1].valid);
  const auto session = lanes.sessions;
  for (int fault = 0; fault < 4; ++fault) {
    model_age = fault == 0 ? 251000000ULL : 0;
    model_event_age = fault == 1 ? 201000000ULL : 0;
    model_valid = fault != 2;
    lane_change = fault == 3 ? cereal::LaneChangeState::STARTING : cereal::LaneChangeState::OFF;
    lanes.update(true); assert(lanes.ready && !lanes.boxes[0].valid && !lanes.boxes[1].valid && lanes.sessions == session);
  }
  model_age = model_event_age = 0; model_valid = true; lane_change = cereal::LaneChangeState::OFF;
  lanes.update(true); assert(lanes.boxes[0].valid && lanes.boxes[1].valid);
  pv5 = false; clock_now += 1100000000ULL; cal_time = dev_time = clock_now;
  lanes.update(true); assert(!lanes.ready && lanes.status == ModelFovStatus::VEHICLE_UNAVAILABLE);
}
'''
  wrapper = tmp_path / 'context.cc'
  wrapper.write_text(fixture + context + scenarios, encoding='utf-8')
  executable = tmp_path / 'context'
  result = subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                  str(wrapper), '-o', str(executable)], capture_output=True)
  assert result.returncode == 0, result.stderr.decode()
  result = subprocess.run([str(executable)], capture_output=True, timeout=10)
  assert result.returncode == 0, result.stderr.decode()
