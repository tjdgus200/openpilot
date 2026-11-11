#include "system/camerad/cameras/camera_common.h"
#include "system/camerad/cameras/spectra.h"
#include "system/camerad/cameras/model_fov_meter.h"

#include <poll.h>
#include <sys/ioctl.h>
#include <sys/resource.h>

#include <algorithm>
#include <cassert>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <deque>
#include <string>
#include <vector>
#include <condition_variable>
#include <system_error>

#include "common/params.h"
#include "common/swaglog.h"


ExitHandler do_exit;

// for debugging
const bool env_debug_frames = getenv("DEBUG_FRAMES") != nullptr;
const bool env_log_raw_frames = getenv("LOG_RAW_FRAMES") != nullptr;
const bool env_ctrl_exp_from_params = getenv("CTRL_EXP_FROM_PARAMS") != nullptr;

class ModelFovContext : public ModelFovSnapshot {
public:

  void update(bool dual_camera) {
    const uint64_t now = nanos_since_boot();
    if (now >= next_params) {
      next_params = now + 250000000ULL;
      const std::string requested = params.get("Pv5ModelFovExposure");
      const int next_mode = requested.size() == 1 && requested[0] >= '0' && requested[0] <= '3' ? requested[0] - '0' : 0;
      const int next_assist = (params.get("Pv5RoadExposureAssist") == "1" ? 1 : 0) |
                              (params.get("Pv5WideExposureAssist") == "1" ? 2 : 0);
      const int next_lane = (params.get("Pv5RoadLaneExposure") == "1" ? 1 : 0) |
                           (params.get("Pv5WideLaneExposure") == "1" ? 2 : 0);
      if (bool(next_lane) != bool(lane_mode)) sm.reset();
      for (int lens = 0; lens < 2; ++lens) {
        const int next_signature = ((next_lane & (1 << lens)) ? 4 : 0) |
          ((next_assist & (1 << lens)) ? 2 : includes_camera(next_mode, lens) ? 1 : 0);
        if (camera_signature(lens) != next_signature) ++sessions[lens];
      }
      mode = next_mode;
      assist_mode = next_assist;
      lane_mode = next_lane;
      enabled = mode != 0 || assist_mode != 0 || lane_mode != 0;
      const std::string wide = enabled ? params.get("UseWideCamera") : "";
      enabled = enabled && (wide.empty() || wide == "1");  // Declared default is ON, as in modeld.
      if (enabled) {
        const double current_yaw = std::strtod(params.get("CameraYawTrimDeg").c_str(), nullptr) * 0.01 * std::acos(-1.0) / 180.0;
        yaw_invalidated |= !std::isfinite(current_yaw) || std::abs(current_yaw - yaw_trim) > 1e-9;
        enabled = !yaw_invalidated;
      }
      const std::string choice = enabled ? params.get("Pv5WideModelFov") : "";
      const int value = choice.size() == 1 && choice[0] >= '0' && choice[0] <= '4' ? choice[0] - '0' : 0;
      if (selection != value) { selection = value; ++generation; lane_wide_changed_at = now; }
      const std::string down = enabled ? params.get("Pv5WideModelDown") : "";
      int next_down = !down.empty() && down.size() <= 2 && std::all_of(down.begin(), down.end(), [](char c) { return c >= '0' && c <= '9'; }) ? std::stoi(down) : 0;
      if (next_down > 30) next_down = 0;
      if (wide_model_down != next_down) { wide_model_down = next_down; ++generation; lane_wide_changed_at = now; }
    }
    ready = false;
    status = ModelFovStatus::INPUT_UNAVAILABLE;
    if (!enabled || !dual_camera || env_ctrl_exp_from_params) {
      status = !(mode || assist_mode || lane_mode) ? ModelFovStatus::DISABLED : yaw_invalidated ? ModelFovStatus::YAW_CHANGED :
        env_ctrl_exp_from_params ? ModelFovStatus::MANUAL_EXPOSURE : ModelFovStatus::UNSUPPORTED;
      sm.reset(); return;
    }
    if (now >= next_vehicle) {
      next_vehicle = now + 1000000000ULL;
      pv5 = false;
      const auto bytes = params.get("CarParams");
      if (!bytes.empty() && bytes.size() <= 1024 * 1024 && bytes.size() % sizeof(capnp::word) == 0) {
        std::vector<capnp::word> words(bytes.size() / sizeof(capnp::word));
        std::memcpy(words.data(), bytes.data(), bytes.size());
        try {
          capnp::FlatArrayMessageReader reader(kj::arrayPtr(words.data(), words.size()));
          const auto cp = reader.getRoot<cereal::CarParams>();
          pv5 = !cp.getNotCar() && cp.getCarFingerprint() == "KIA_PV5";
        } catch (const kj::Exception &) { pv5 = false; }
      }
    }
    if (!pv5) { status = ModelFovStatus::VEHICLE_UNAVAILABLE; return; }
    if (!sm) sm = std::make_unique<SubMaster>(lane_mode ? std::vector<const char *>{"liveCalibration", "deviceState", "modelV2"} :
                                                       std::vector<const char *>{"liveCalibration", "deviceState"});
    sm->update(0);  // Only the latest available data; never wait for modeld/Jetson.
    if (!sm->allAliveAndValid({"liveCalibration", "deviceState"})) return;
    const auto &cal_event = (*sm)["liveCalibration"];
    const auto &dev_event = (*sm)["deviceState"];
    if (now < cal_event.getLogMonoTime() || now - cal_event.getLogMonoTime() > 1000000000ULL ||
        now < dev_event.getLogMonoTime() || now - dev_event.getLogMonoTime() > 2500000000ULL) {
      status = now < cal_event.getLogMonoTime() || now < dev_event.getLogMonoTime() ? ModelFovStatus::TIMESTAMP_INVALID :
        now - cal_event.getLogMonoTime() > 1000000000ULL ? ModelFovStatus::CALIBRATION_EXPIRED : ModelFovStatus::DEVICE_EXPIRED;
      return;
    }
    const auto device = dev_event.getDeviceState().getDeviceType();
    if (device != cereal::InitData::DeviceType::TICI && device != cereal::InitData::DeviceType::TIZI) {
      status = ModelFovStatus::UNSUPPORTED; return;
    }
    const auto calibration = cal_event.getLiveCalibration();
    const auto angles = calibration.getRpyCalib();
    if (calibration.getCalStatus() != cereal::LiveCalibrationData::Status::CALIBRATED || angles.size() != 3) {
      status = calibration.getCalStatus() != cereal::LiveCalibrationData::Status::CALIBRATED ?
        ModelFovStatus::CALIBRATION_PENDING : ModelFovStatus::CALIBRATION_INVALID;
      return;
    }
    if (sm->updated("liveCalibration")) {
      for (int i = 0; i < 3; ++i) rpy[i] = angles[i];
      // Use the startup yaw snapshot; a later stored change invalidates metering.
      // modeld polls yaw more slowly. Never use the display-only pitch preview.
      rpy[2] -= yaw_trim;
      ++generation;
    }
    for (double angle : rpy) if (!std::isfinite(angle) || std::abs(angle) > 0.5) {
      status = ModelFovStatus::CALIBRATION_INVALID; return;
    }
    valid_until = std::min(cal_event.getLogMonoTime() + 1000000000ULL, dev_event.getLogMonoTime() + 2500000000ULL);
    boxes = {};
    if (lane_mode && sm->allAliveAndValid({"modelV2"})) {
      const auto &event = (*sm)["modelV2"];
      const auto model = event.getModelV2();
      const uint64_t eof = model.getTimestampEof();
      // Original camera time, not reception time: repeats cannot renew old lanes.
      if (eof && now >= eof && now - eof <= 250000000ULL && now >= event.getLogMonoTime() &&
          now - event.getLogMonoTime() <= 200000000ULL &&
          model.getMeta().getLaneChangeState() == cereal::LaneChangeState::OFF) {
        const auto lines = model.getLaneLines();
        const auto probabilities = model.getLaneLineProbs(), deviations = model.getLaneLineStds();
        if (lines.size() == 4 && probabilities.size() == 4 && deviations.size() == 4) {
          ExposureLaneInput input;
          const auto l = lines[1], r = lines[2];
          const auto count = l.getX().size();
          if (count >= 6 && count <= 33 && l.getY().size() == count && l.getZ().size() == count &&
              r.getX().size() == count && r.getY().size() == count && r.getZ().size() == count) {
            input.count = count;
            for (unsigned int i = 0; i < count; ++i) {
              input.left[i] = {l.getX()[i], l.getY()[i], l.getZ()[i]};
              input.right[i] = {r.getX()[i], r.getY()[i], r.getZ()[i]};
            }
            input.probability = {probabilities[1], probabilities[2]};
            input.deviation = {deviations[1], deviations[2]};
            for (int lens = 0; lens < 2; ++lens) {
              // modeld polls FOV independently. Let previous-geometry outputs
              // expire before using lane predictions in the newly selected view.
              if (lens && now - lane_wide_changed_at < 500000000ULL) continue;
              boxes[lens] = exposure_box_target(input, lens, selection, rpy);
              boxes[lens].valid_until = std::min<uint64_t>(valid_until, eof + 250000000ULL);
            }
          }
        }
      }
    }
    ready = true;
    status = ModelFovStatus::ACTIVE;
  }

private:
  Params params;
  // Match modeld's CameraYawTrimDeg convention/units at camera startup.
  // On a stored change, fall back until camera/model restart rather than
  // guessing when modeld's 100-loop settings poll has taken effect.
  const double yaw_trim = std::strtod(params.get("CameraYawTrimDeg").c_str(), nullptr) * 0.01 * std::acos(-1.0) / 180.0;
  std::unique_ptr<SubMaster> sm;
  uint64_t next_params = 0;
  uint64_t next_vehicle = 0;
  uint64_t lane_wide_changed_at = 0;
  bool enabled = false;
  bool pv5 = false;
  bool yaw_invalidated = false;
};

// Disk reads, Cap'n Proto parsing and IPC polling stay off the camera owner.
// Camera delivery reads one small immutable latest-value snapshot, never joins.
class ModelFovWorker {
public:
  explicit ModelFovWorker(bool dual_camera) {
    try {
      worker = std::thread([this, dual_camera]() {
        try {
          // camerad may be real-time. Never let this worker inherit that policy.
          sched_param policy = {};
          if (pthread_setschedparam(pthread_self(), SCHED_OTHER, &policy) != 0 || setpriority(PRIO_PROCESS, 0, 10) != 0) return;
          ModelFovContext context;
          std::unique_lock<std::mutex> lock(mutex);
          while (running) {
            context.ready = false;
            try { context.update(dual_camera); }
            catch (const kj::Exception &) { context.ready = false; context.status = ModelFovStatus::WORKER_ERROR; }
            catch (const std::exception &) { context.ready = false; context.status = ModelFovStatus::WORKER_ERROR; }
            snapshot.publish(context);
            wake.wait_for(lock, std::chrono::milliseconds(context.lane_mode ? 100 : 250), [this]() { return !running; });
          }
        } catch (const kj::Exception &) { }
        catch (const std::exception &) { }
        // Worker initialization failure leaves stock metering in effect.
      });
    } catch (const std::system_error &) {
      // Optional worker unavailable: retain the default stock-only snapshot.
    }
  }
  ~ModelFovWorker() {
    { std::lock_guard<std::mutex> lock(mutex); running = false; }
    wake.notify_one();
    if (worker.joinable()) worker.join();  // Shutdown only, outside the camera frame loop.
  }
  ModelFovSnapshot latest() {
    snapshot.read(last);  // On overlap, retain only the previous unexpired value.
    ModelFovSnapshot result = last;
    if (nanos_since_boot() >= result.valid_until) {
      if (result.ready) result.status = ModelFovStatus::INPUT_EXPIRED;
      result.ready = false;
    }
    return result;
  }
private:
  ModelFovMailbox snapshot;
  ModelFovSnapshot last;
  std::thread worker;
  std::mutex mutex;
  std::condition_variable wake;
  bool running = true;
};


class CameraState {
public:
  SpectraCamera camera;
  int exposure_time = 5;
  bool dc_gain_enabled = false;
  int dc_gain_weight = 0;
  int gain_idx = 0;
  float analog_gain_frac = 0;

  float cur_ev[3] = {};
  float best_ev_score = 0;
  int new_exp_g = 0;
  int new_exp_t = 0;

  Rect ae_xywh = {};
  float measured_grey_fraction = 0;
  float target_grey_fraction = 0.125;

  float fl_pix = 0;
  std::unique_ptr<PubMaster> pm;
  std::deque<cam_req_mgr_message> queued_events;
  ModelFovMeter fov_meter;
  ModelFovStability fov_stability;
  uint64_t fov_generation = UINT64_MAX;
  ModelFovRegion fov_region;
  bool fov_configured = false;

  CameraState(SpectraMaster *master, const CameraConfig &config) : camera(master, config) {};
  ~CameraState();
  void init(VisionIpcServer *v);
  void update_exposure_score(float desired_ev, int exp_t, int exp_g_idx, float exp_gain);
  void set_camera_exposure(float grey_frac);
  void set_exposure_rect();
  void sendState(const ModelFovSnapshot &context);

  float get_gain_factor() const {
    return (1 + dc_gain_weight * (camera.sensor->dc_gain_factor-1) / camera.sensor->dc_gain_max_weight);
  }
};

void CameraState::init(VisionIpcServer *v) {
  camera.camera_open(v);

  if (!camera.enabled) return;

  fl_pix = camera.cc.focal_len / camera.sensor->pixel_size_mm / camera.sensor->out_scale;
  set_exposure_rect();

  dc_gain_weight = camera.sensor->dc_gain_min_weight;
  gain_idx = camera.sensor->analog_gain_rec_idx;
  cur_ev[0] = cur_ev[1] = cur_ev[2] = get_gain_factor() * camera.sensor->sensor_analog_gains[gain_idx] * exposure_time;

  pm = std::make_unique<PubMaster>(std::vector{camera.cc.publish_name});
}

CameraState::~CameraState() {}

void CameraState::set_exposure_rect() {
  // set areas for each camera, shouldn't be changed
  std::vector<std::pair<Rect, float>> ae_targets = {
    // (Rect, F)
    std::make_pair((Rect){96, 400, 1734, 524}, 567.0),  // wide
    std::make_pair((Rect){96, 160, 1734, 986}, 2648.0), // road
    std::make_pair((Rect){96, 242, 1736, 906}, 567.0)   // driver
  };
  int h_ref = 1208;
  /*
    exposure target intrinsics is
    [
      [F, 0, 0.5*ae_xywh[2]]
      [0, F, 0.5*H-ae_xywh[1]]
      [0, 0, 1]
    ]
  */
  auto ae_target = ae_targets[camera.cc.camera_num];
  Rect xywh_ref = ae_target.first;
  float fl_ref = ae_target.second;

  ae_xywh = (Rect){
    std::max(0, (int)camera.buf.out_img_width / 2 - (int)(fl_pix / fl_ref * xywh_ref.w / 2)),
    std::max(0, (int)camera.buf.out_img_height / 2 - (int)(fl_pix / fl_ref * (h_ref / 2 - xywh_ref.y))),
    std::min((int)(fl_pix / fl_ref * xywh_ref.w), (int)camera.buf.out_img_width / 2 + (int)(fl_pix / fl_ref * xywh_ref.w / 2)),
    std::min((int)(fl_pix / fl_ref * xywh_ref.h), (int)camera.buf.out_img_height / 2 + (int)(fl_pix / fl_ref * (h_ref / 2 - xywh_ref.y)))
  };
}

void CameraState::update_exposure_score(float desired_ev, int exp_t, int exp_g_idx, float exp_gain) {
  float score = camera.sensor->getExposureScore(desired_ev, exp_t, exp_g_idx, exp_gain, gain_idx);
  if (score < best_ev_score) {
    new_exp_t = exp_t;
    new_exp_g = exp_g_idx;
    best_ev_score = score;
  }
}

void CameraState::set_camera_exposure(float grey_frac) {
  if (!camera.enabled) return;
  std::vector<double> target_grey_minimums = {0.1, 0.1, 0.125}; // wide, road, driver

  const float dt = 0.05;

  const float ts_grey = 10.0;
  const float ts_ev = 0.05;

  const float k_grey = (dt / ts_grey) / (1.0 + dt / ts_grey);
  const float k_ev = (dt / ts_ev) / (1.0 + dt / ts_ev);

  // It takes 3 frames for the commanded exposure settings to take effect. The first frame is already started by the time
  // we reach this function, the other 2 are due to the register buffering in the sensor.
  // Therefore we use the target EV from 3 frames ago, the grey fraction that was just measured was the result of that control action.
  // TODO: Lower latency to 2 frames, by using the histogram outputted by the sensor we can do AE before the debayering is complete

  const auto &sensor = camera.sensor;
  // Offset idx by one to not get stuck in self loop
  const float cur_ev_ = cur_ev[(camera.buf.cur_frame_data.frame_id - 1) % 3] * sensor->ev_scale;

  // Scale target grey between min and 0.4 depending on lighting conditions
  float new_target_grey = std::clamp(0.4 - 0.3 * log2(1.0 + sensor->target_grey_factor*cur_ev_) / log2(6000.0), target_grey_minimums[camera.cc.camera_num], 0.4);
  float target_grey = (1.0 - k_grey) * target_grey_fraction + k_grey * new_target_grey;

  float desired_ev = std::clamp(cur_ev_ / sensor->ev_scale * target_grey / grey_frac, sensor->min_ev, sensor->max_ev);
  float k = (1.0 - k_ev) / 3.0;
  desired_ev = (k * cur_ev[0]) + (k * cur_ev[1]) + (k * cur_ev[2]) + (k_ev * desired_ev);

  best_ev_score = 1e6;
  new_exp_g = 0;
  new_exp_t = 0;

  // Hysteresis around high conversion gain
  // We usually want this on since it results in lower noise, but turn off in very bright day scenes
  bool enable_dc_gain = dc_gain_enabled;
  if (!enable_dc_gain && target_grey < sensor->dc_gain_on_grey) {
    enable_dc_gain = true;
    dc_gain_weight = sensor->dc_gain_min_weight;
  } else if (enable_dc_gain && target_grey > sensor->dc_gain_off_grey) {
    enable_dc_gain = false;
    dc_gain_weight = sensor->dc_gain_max_weight;
  }

  if (enable_dc_gain && dc_gain_weight < sensor->dc_gain_max_weight) {dc_gain_weight += 1;}
  if (!enable_dc_gain && dc_gain_weight > sensor->dc_gain_min_weight) {dc_gain_weight -= 1;}

  std::string gain_bytes, time_bytes;
  if (env_ctrl_exp_from_params) {
    static Params params;
    gain_bytes = params.get("CameraDebugExpGain");
    time_bytes = params.get("CameraDebugExpTime");
  }

  if (gain_bytes.size() > 0 && time_bytes.size() > 0) {
    // Override gain and exposure time
    gain_idx = std::stoi(gain_bytes);
    exposure_time = std::stoi(time_bytes);

    new_exp_g = gain_idx;
    new_exp_t = exposure_time;
    enable_dc_gain = false;
  } else {
    // Simple brute force optimizer to choose sensor parameters to reach desired EV
    int min_g = std::max(gain_idx - 1, sensor->analog_gain_min_idx);
    int max_g = std::min(gain_idx + 1, sensor->analog_gain_max_idx);
    for (int g = min_g; g <= max_g; g++) {
      float gain = sensor->sensor_analog_gains[g] * get_gain_factor();

      // Compute optimal time for given gain
      int t = std::clamp(int(std::round(desired_ev / gain)), sensor->exposure_time_min, sensor->exposure_time_max);

      // Only go below recommended gain when absolutely necessary to not overexpose
      if (g < sensor->analog_gain_rec_idx && t > 20 && g < gain_idx) {
        continue;
      }

      update_exposure_score(desired_ev, t, g, gain);
    }
  }

  measured_grey_fraction = grey_frac;
  target_grey_fraction = target_grey;

  analog_gain_frac = sensor->sensor_analog_gains[new_exp_g];
  gain_idx = new_exp_g;
  exposure_time = new_exp_t;
  dc_gain_enabled = enable_dc_gain;

  float gain = analog_gain_frac * get_gain_factor();
  cur_ev[camera.buf.cur_frame_data.frame_id % 3] = exposure_time * gain;

  // LOGE("ae - camera %d, cur_t %.5f, sof %.5f, dt %.5f", camera.cc.camera_num, 1e-9 * nanos_since_boot(), 1e-9 * camera.buf.cur_frame_data.timestamp_sof, 1e-9 * (nanos_since_boot() - camera.buf.cur_frame_data.timestamp_sof));

  auto exp_reg_array = sensor->getExposureRegisters(exposure_time, new_exp_g, dc_gain_enabled);
  camera.sensors_i2c(exp_reg_array.data(), exp_reg_array.size(), CAM_SENSOR_PACKET_OPCODE_SENSOR_CONFIG, camera.sensor->data_word);
}

void CameraState::sendState(const ModelFovSnapshot &context) {
  camera.buf.sendFrameToVipc();

  MessageBuilder msg;
  auto framed = (msg.initEvent().*camera.cc.init_camera_state)();
  const FrameMetadata &meta = camera.buf.cur_frame_data;
  framed.setFrameId(meta.frame_id);
  framed.setRequestId(meta.request_id);
  framed.setTimestampEof(meta.timestamp_eof);
  framed.setTimestampSof(meta.timestamp_sof);
  framed.setIntegLines(exposure_time);
  framed.setGain(analog_gain_frac * get_gain_factor());
  framed.setHighConversionGain(dc_gain_enabled);
  framed.setMeasuredGreyFraction(measured_grey_fraction);
  framed.setTargetGreyFraction(target_grey_fraction);
  framed.setProcessingTime(meta.processing_time);

  const float ev = cur_ev[meta.frame_id % 3];
  const float perc = util::map_val(ev, camera.sensor->min_ev, camera.sensor->max_ev, 0.0f, 100.0f);
  framed.setExposureValPercent(perc);
  framed.setSensor(camera.sensor->image_sensor);

  // Log raw frames for road camera
  if (env_log_raw_frames && camera.cc.stream_type == VISION_STREAM_ROAD && meta.frame_id % 100 == 5) {  // no overlap with qlog decimation
    framed.setImage(get_raw_frame_image(&camera.buf));
  }

  const bool front = camera.cc.stream_type == VISION_STREAM_ROAD || camera.cc.stream_type == VISION_STREAM_WIDE_ROAD;
  const bool known_sensor = camera.sensor->image_sensor == cereal::FrameData::ImageSensor::AR0231 ||
                            camera.sensor->image_sensor == cereal::FrameData::ImageSensor::OX03C10;
  const uint64_t now = nanos_since_boot();
  const bool wide = camera.cc.stream_type == VISION_STREAM_WIDE_ROAD;
  fov_stability.set_session(context.sessions[wide]);
  const bool worker_unavailable = context.status == ModelFovStatus::WORKER_UNAVAILABLE || context.status == ModelFovStatus::WORKER_ERROR;
  ModelFovStatus fov_status = !front ? ModelFovStatus::DISABLED : worker_unavailable ? context.status :
    !context.camera_enabled(wide) ? ModelFovStatus::DISABLED : !known_sensor ? ModelFovStatus::UNSUPPORTED : context.status;
  if (context.ready && now >= context.valid_until && front && context.camera_enabled(wide) && known_sensor) {
    fov_status = ModelFovStatus::INPUT_EXPIRED;
  }
  bool fov_valid = false;
  if (context.ready && context.camera_enabled(wide) && now < context.valid_until && front && known_sensor && !fov_stability.locked()) {
    const ModelFovRegion region = context.camera_lane(wide) && !context.camera_fov_base(wide) ?
      ModelFovRegion{ae_xywh.x, ae_xywh.y, ae_xywh.w, ae_xywh.h} : ModelFovRegion{};
    if (fov_generation != context.generation || region != fov_region) {
      fov_region = region;
      fov_generation = context.generation;
      fov_configured = fov_meter.configure(camera.buf.out_img_width, camera.buf.out_img_height,
                                          camera.cc.stream_type == VISION_STREAM_WIDE_ROAD, context.rpy, context.selection,
                                          camera.buf.cur_yuv_buf->stride, region, context.wide_model_down);
    }
    fov_valid = fov_configured && fov_meter.measure(camera.buf.cur_yuv_buf->y, camera.buf.cur_yuv_buf->len,
                                                 context.camera_assisted(wide),
                                                 context.camera_lane(wide) ? &context.boxes[wide] : nullptr, now);
    fov_status = fov_meter.status;
  }
  const bool fov_active = fov_stability.use_fov(fov_valid, now);
  if (fov_stability.locked()) fov_status = fov_stability.lock_status();
  else if (fov_valid) fov_status = fov_active ? ModelFovStatus::ACTIVE : ModelFovStatus::WARMING;
  framed.setPv5ExposureFovStatus(static_cast<uint8_t>(fov_status));
  framed.setPv5ExposureFovWarmupMs(fov_stability.warmup_ms());
  if (fov_active) {
    framed.setPv5ExposureFovActive(true);
    auto polygon = framed.initPv5ExposureFovPolygon(fov_meter.polygon.size());
    for (size_t i = 0; i < fov_meter.polygon.size(); ++i) polygon.set(i, fov_meter.polygon[i]);
    framed.setPv5ExposureBlackFraction(fov_meter.black_fraction);
    framed.setPv5ExposureWhiteFraction(fov_meter.white_fraction);
  }
  // Box motion adds a bounded whole-region correction to the selected base.
  // With both base toggles OFF, the base is the original stock ROI/controller.
  const float base_grey = fov_active && context.camera_lane(wide) && !context.camera_fov_base(wide) ?
    calculate_exposure_value(&camera.buf, ae_xywh, 2, 2) : fov_meter.grey;
  const float metered_grey = fov_meter.assisted_grey(fov_active && (context.camera_assisted(wide) || context.camera_lane(wide)), now, base_grey);
  const float grey = fov_active ? metered_grey :
    calculate_exposure_value(&camera.buf, ae_xywh, 2, camera.cc.stream_type != VISION_STREAM_DRIVER ? 2 : 4);
  set_camera_exposure(grey);

  // Send the message
  pm->send(camera.cc.publish_name, msg);
}

void camerad_thread() {
  // TODO: centralize enabled handling

  VisionIpcServer v("camerad");

  // *** initial ISP init ***
  SpectraMaster m;
  m.init();

  // *** per-cam init ***
  std::vector<std::unique_ptr<CameraState>> cams;
  for (const auto &config : ALL_CAMERA_CONFIGS) {
    auto cam = std::make_unique<CameraState>(&m, config);
    cam->init(&v);
    cams.emplace_back(std::move(cam));
  }

  v.start_listener();

  // start devices
  LOG("-- Starting devices");
  for (auto &cam : cams) cam->camera.sensors_start();

  // poll events
  LOG("-- Dequeueing Video events");
  bool road_enabled = false, wide_enabled = false;
  for (auto &cam : cams) {
    road_enabled |= cam->camera.enabled && cam->camera.cc.stream_type == VISION_STREAM_ROAD;
    wide_enabled |= cam->camera.enabled && cam->camera.cc.stream_type == VISION_STREAM_WIDE_ROAD;
  }
  ModelFovWorker fov_worker(road_enabled && wide_enabled);
  while (!do_exit) {
    // Driver IFE/BPS waits must not stall road/wide event delivery. Its result
    // wakes this same owner thread, which still owns all camera state/recovery.
    std::vector<pollfd> fds = {{.fd = m.video0_fd, .events = POLLPRI}};
    for (auto &cam : cams) {
      fds.push_back({.fd = cam->camera.frame_wait_fd(), .events = POLLIN});
    }
    int ret = poll(fds.data(), fds.size(), 1000);
    if (ret < 0) {
      if (errno == EINTR || errno == EAGAIN) continue;
      LOGE("poll failed (%d - %d)", ret, errno);
      break;
    }
    const auto fov_context = fov_worker.latest();

    for (size_t i = 0; i < cams.size(); ++i) {
      if ((fds[i + 1].revents & POLLIN) && cams[i]->camera.complete_frame_wait()) {
        cams[i]->sendState(fov_context);
      }
    }

    if (fds[0].revents & POLLPRI) {
      struct v4l2_event ev = {0};
      ret = HANDLE_EINTR(ioctl(fds[0].fd, VIDIOC_DQEVENT, &ev));
      if (ret == 0) {
        if (ev.type == V4L_EVENT_CAM_REQ_MGR_EVENT) {
          struct cam_req_mgr_message *event_data = (struct cam_req_mgr_message *)ev.u.data;
          if (env_debug_frames) {
            printf("sess_hdl 0x%6X, link_hdl 0x%6X, frame_id %lu, req_id %lu, timestamp %.2f ms, sof_status %d\n", event_data->session_hdl, event_data->u.frame_msg.link_hdl,
                   event_data->u.frame_msg.frame_id, event_data->u.frame_msg.request_id, event_data->u.frame_msg.timestamp/1e6, event_data->u.frame_msg.sof_status);
            do_exit = do_exit || event_data->u.frame_msg.frame_id > (1*20);
          }

          for (auto &cam : cams) {
            if (event_data->session_hdl == cam->camera.session_handle) {
              if (cam->camera.cc.stream_type == VISION_STREAM_DRIVER) {
                // Bound backlog during a failed fence. A discarded event is
                // detected by the existing raw/request-ID gap recovery.
                if (cam->queued_events.size() >= 32) {
                  cam->queued_events.pop_front();
                  LOGE("driver camera event backlog overflow");
                }
                cam->queued_events.push_back(*event_data);
              } else if (cam->camera.handle_camera_event(event_data)) {
                cam->sendState(fov_context);
              }
              break;
            }
          }
        } else {
          LOGE("unhandled event %d\n", ev.type);
        }
      } else {
        LOGE("VIDIOC_DQEVENT failed, errno=%d", errno);
      }
    }
    for (auto &cam : cams) {
      while (!cam->camera.frame_wait_pending() && !cam->queued_events.empty()) {
        const auto event = cam->queued_events.front();
        cam->queued_events.pop_front();
        if (cam->camera.handle_camera_event(&event)) cam->sendState(fov_context);
      }
    }
  }
}
