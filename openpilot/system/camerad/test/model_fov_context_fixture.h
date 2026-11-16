// Deterministic Params/cereal/SubMaster boundary for the production context.

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
