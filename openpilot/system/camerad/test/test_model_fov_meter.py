"""PC-only native sampler/warp equivalence, bounds and capture wiring tests."""
import ctypes
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import get_warp_matrix, wider_model_warp

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
      value.generation = i; value.valid_until = i + 1;
      value.rpy = {double(i), double(i * 2), double(i * 3)};
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
              value.rpy[0] == i && value.rpy[1] == i * 2 && value.rpy[2] == i * 3;
    }
  } while (!done.load());
  writer.join();
  ModelFovSnapshot last;
  return good && mailbox.read(last) && last.generation == 99999;
}
extern "C" int meter(int width, int height, bool wide, double roll, double pitch, double yaw,
                     int selection, int stride, const uint8_t *pixels, size_t size, float *output) {
  ModelFovMeter meter;
  if (!meter.configure(width, height, wide, {roll, pitch, yaw}, selection, stride)) return 0;
  for (int i = 0; i < 8; ++i) output[i] = meter.polygon[i];
  if (!pixels) return 1;
  if (!meter.measure(pixels, size)) return -1;
  output[8] = meter.grey; output[9] = meter.black_fraction; output[10] = meter.white_fraction;
  return 2;
}
''', encoding='utf-8')
  library = folder / 'meter.so'
  subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared', '-pthread',
                  '-I', str(ROOT), str(wrapper), '-o', str(library)], check=True, capture_output=True)
  module = ctypes.CDLL(str(library))
  func = module.meter
  func.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_double, ctypes.c_double, ctypes.c_double,
                   ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_float)]
  func.restype = ctypes.c_int

  def call(wide=True, rpy=(0., 0., 0.), selection=0, image=None, size=None, dimensions=(1928, 1208), stride=0):
    output = (ctypes.c_float * 11)()
    status = func(*dimensions, wide, *rpy, selection, stride or (image.strides[0] if image is not None else 0),
                  image.ctypes.data if image is not None else None,
                  image.nbytes if size is None and image is not None else size or 0, output)
    return status, np.array(output)
  call.mailbox_check = module.mailbox_check
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


def test_latest_snapshot_cannot_mix_calibration_or_fov_generations(native):
  assert native.mailbox_check() == 1


def test_camera_owner_keeps_ipc_and_disk_reads_out_of_frame_delivery():
  source = (ROOT / 'system/camerad/cameras/camera_qcom2.cc').read_text(encoding='utf-8')
  delivery = source.split('void CameraState::sendState(', 1)[1].split('void camerad_thread()', 1)[0]
  frame_loop = source.split('ModelFovWorker fov_worker(', 1)[1]
  assert 'params.get' not in delivery and 'sm->update' not in delivery
  assert 'fov_worker.latest()' in frame_loop and 'context.update(' not in frame_loop
  assert 'SCHED_OTHER' in source and 'setpriority(PRIO_PROCESS, 0, 10)' in source
  assert 'fov_active ? fov_meter.grey :' in delivery and 'calculate_exposure_value(' in delivery


def test_default_off_and_camera_menu_keep_display_separate_from_metering():
  settings = json.loads((ROOT / 'selfdrive/carrot_settings.json').read_text(encoding='utf-8'))
  params = {item['name']: item for item in settings['params']}
  assert params['Pv5ModelFovExposure']['default'] == params['Pv5CameraFovPreview']['default'] == 0
  category = next(c for c in settings['menu'] if c['id'] == 'PV5')
  camera = next(g for g in category['groups'] if g['id'] == 'PV5_CAMERA')
  assert camera['groups'][0]['params'] == ['Pv5WideModelFov', 'Pv5ModelFovExposure']
  assert camera['groups'][1]['params'] == ['Pv5CameraFovPreview', 'Pv5FovPreviewPitch']


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
}
bool not_car = false, pv5 = true, messages_valid = true;
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
struct Event {
  bool cal = false;
  uint64_t getLogMonoTime() const { return cal ? cal_time : dev_time; }
  Calibration getLiveCalibration() const { return {}; }
  Device getDeviceState() const { return {}; }
};
struct SubMaster {
  explicit SubMaster(std::vector<const char *>) {}
  void update(int timeout) { assert(timeout == 0); }
  bool allAliveAndValid(std::vector<const char *>) { return messages_valid; }
  bool updated(const char *) { return true; }
  Event &operator[](const char *key) { static Event cal{true}, dev{false}; return std::string(key) == "liveCalibration" ? cal : dev; }
};
'''
  scenarios = r'''
int main() {
  for (int scenario = 0; scenario < 20; ++scenario) {
    clock_now = 10000000000ULL; cal_time = dev_time = clock_now;
    values = {{"Pv5ModelFovExposure", "1"}, {"UseWideCamera", "1"}, {"Pv5WideModelFov", "4"},
              {"CameraYawTrimDeg", "0"}, {"CarParams", "12345678"}};
    not_car = false; pv5 = true; messages_valid = true; env_ctrl_exp_from_params = false;
    angles = {0, 0, 0}; device = cereal::InitData::DeviceType::TIZI;
    calibrated = cereal::LiveCalibrationData::Status::CALIBRATED;
    ModelFovContext context;
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
    if (scenario == 0 || scenario >= 16) {
      assert(context.ready && context.selection == 4);
      if (scenario == 16) {
        values["Pv5ModelFovExposure"] = "0"; clock_now += 250000000ULL;
        context.update(true); assert(!context.ready);
      } else if (scenario == 17) {
        clock_now += 1100000000ULL; context.update(true); assert(!context.ready);
      } else if (scenario == 19) {
        values["CameraYawTrimDeg"] = "100"; clock_now += 250000000ULL;
        context.update(true); assert(!context.ready);
        values["CameraYawTrimDeg"] = "0"; clock_now += 250000000ULL;
        context.update(true); assert(!context.ready);
      }
    } else assert(!context.ready);
  }
}
'''
  wrapper = tmp_path / 'context.cc'
  wrapper.write_text(fixture + context + scenarios, encoding='utf-8')
  executable = tmp_path / 'context'
  subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                  str(wrapper), '-o', str(executable)], check=True, capture_output=True)
  subprocess.run([str(executable)], check=True, capture_output=True, timeout=10)
