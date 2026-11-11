#pragma once

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>

#include "lane_exposure.h"

// Stable diagnostic codes for FrameData.pv5ExposureFovStatus. Never a control input.
enum class ModelFovStatus : uint8_t {
  UNKNOWN = 0, DISABLED = 1, ACTIVE = 2, WARMING = 3, INPUT_UNAVAILABLE = 4,
  CALIBRATION_EXPIRED = 5, DEVICE_EXPIRED = 6, CALIBRATION_INVALID = 7,
  CALIBRATION_PENDING = 8, UNSUPPORTED = 9, MANUAL_EXPOSURE = 10, YAW_CHANGED = 11,
  REGION_CLIPPED = 12, INVALID_BUFFER = 13, INVALID_BRIGHTNESS = 14,
  REPEATED_FALLBACK = 15, CLOCK_INVALID = 16, WORKER_UNAVAILABLE = 17,
  WORKER_ERROR = 18, TIMESTAMP_INVALID = 19, VEHICLE_UNAVAILABLE = 20, INPUT_EXPIRED = 21,
};

struct ModelFovSnapshot {
  bool ready = false;
  int selection = 0, mode = 0, assist_mode = 0, lane_mode = 0;  // bits: road=1, wide=2
  int wide_model_down = 0;  // Tenths of a degree, actual virtual wide input only.
  uint64_t generation = 0, valid_until = 0;
  std::array<uint64_t, 2> sessions = {};  // road, wide; unrelated option edges cannot rearm the other camera.
  std::array<double, 3> rpy = {};
  std::array<ExposureBoxTarget, 2> boxes = {};
  ModelFovStatus status = ModelFovStatus::WORKER_UNAVAILABLE;

  // Preserve the original persisted 0/OFF and 1/both-ON values.
  static bool includes_camera(int choice, bool wide) { return choice == 1 || choice == (wide ? 3 : 2); }
  bool camera_assisted(bool wide) const { return assist_mode & (wide ? 2 : 1); }
  bool camera_lane(bool wide) const { return lane_mode & (wide ? 2 : 1); }
  bool camera_fov_base(bool wide) const { return camera_assisted(wide) || includes_camera(mode, wide); }
  int camera_signature(bool wide) const { return (camera_lane(wide) ? 4 : 0) | (camera_assisted(wide) ? 2 : includes_camera(mode, wide) ? 1 : 0); }
  int camera_method(bool wide) const { return camera_lane(wide) ? 3 : camera_assisted(wide) ? 2 : includes_camera(mode, wide) ? 1 : 0; }
  bool camera_enabled(bool wide) const { return camera_method(wide) != 0; }
};

// One writer, one reader. One bounded read attempt, never spin behind the worker.
// Atomic fields avoid data races even when a read overlaps a publication.
class ModelFovMailbox {
public:
  ModelFovMailbox() {
    for (auto &angle : rpy) angle.store(0.0);
    for (auto &counter : sessions) counter.store(0);
  }
  void publish(const ModelFovSnapshot &value) {
    ++version;
    ready.store(value.ready);
    selection.store(value.selection);
    wide_model_down.store(value.wide_model_down);
    mode.store(value.mode);
    assist_mode.store(value.assist_mode);
    lane_mode.store(value.lane_mode);
    if (value.lane_mode) for (int lens = 0; lens < 2; ++lens) {
      lane_valid[lens].store(value.boxes[lens].valid);
      lane_until[lens].store(value.boxes[lens].valid_until);
      lane_center[lens].store(value.boxes[lens].center_x);
    }
    status.store(static_cast<int>(value.status));
    generation.store(value.generation);
    valid_until.store(value.valid_until);
    for (int i = 0; i < 2; ++i) sessions[i].store(value.sessions[i]);
    for (int i = 0; i < 3; ++i) rpy[i].store(value.rpy[i]);
    ++version;
  }
  bool read(ModelFovSnapshot &value) const {
    const auto before = version.load();
    if (before & 1) return false;
    ModelFovSnapshot next;
    next.ready = ready.load();
    next.selection = selection.load();
    next.wide_model_down = wide_model_down.load();
    next.mode = mode.load();
    next.assist_mode = assist_mode.load();
    next.lane_mode = lane_mode.load();
    if (next.lane_mode) for (int lens = 0; lens < 2; ++lens) {
      next.boxes[lens].valid = lane_valid[lens].load();
      next.boxes[lens].valid_until = lane_until[lens].load();
      next.boxes[lens].center_x = lane_center[lens].load();
    }
    next.status = static_cast<ModelFovStatus>(status.load());
    next.generation = generation.load();
    next.valid_until = valid_until.load();
    for (int i = 0; i < 2; ++i) next.sessions[i] = sessions[i].load();
    for (int i = 0; i < 3; ++i) next.rpy[i] = rpy[i].load();
    if (before != version.load()) return false;
    value = next;
    return true;
  }
private:
  static_assert(std::atomic<double>::is_always_lock_free && std::atomic<uint64_t>::is_always_lock_free && std::atomic<float>::is_always_lock_free);
  std::atomic<uint64_t> version{0}, generation{0}, valid_until{0};
  std::array<std::atomic<uint64_t>, 2> sessions{};
  std::atomic<bool> ready{false};
  std::atomic<int> selection{0}, mode{0}, assist_mode{0}, lane_mode{0};
  std::atomic<int> wide_model_down{0};
  std::array<std::atomic<bool>, 2> lane_valid{};
  std::array<std::atomic<uint64_t>, 2> lane_until{};
  std::array<std::atomic<float>, 2> lane_center{};
  std::atomic<int> status{static_cast<int>(ModelFovStatus::WORKER_UNAVAILABLE)};
  std::array<std::atomic<double>, 3> rpy{};
};

// Camera-owner state only. Invalid input exits immediately; recovery must prove
// continuous valid frames. No sleep, allocation or changes to the AE controller.
class ModelFovStability {
public:
  static constexpr uint64_t RECOVERY_NS = 2000000000ULL;
  static constexpr uint64_t MAX_WARMUP_GAP_NS = 250000000ULL;
  static constexpr uint64_t FAILURE_WINDOW_NS = 30000000000ULL;

  void set_session(uint64_t value) {
    // Only an observed user option edge changes the session. Calibration/FOV
    // generations and transient failures must not clear a repeated-failure latch.
    if (value != session) {
      *this = ModelFovStability();
      session = value;
    }
  }
  bool locked() const { return latched; }
  ModelFovStatus lock_status() const { return clock_invalid ? ModelFovStatus::CLOCK_INVALID : ModelFovStatus::REPEATED_FALLBACK; }
  uint16_t warmup_ms() const {
    return warming && !active && !latched && last_observed >= healthy_since ?
      static_cast<uint16_t>(std::min((last_observed - healthy_since) / 1000000ULL, 2000ULL)) : 0;
  }

  bool use_fov(bool valid, uint64_t now) {
    if (latched) return false;
    if (observed && now < last_observed) {
      clock_invalid = true;
      active = false;
      latched = true;  // A clock discontinuity cannot prove a recovery interval.
      return false;
    }
    const bool gap = observed && now - last_observed > MAX_WARMUP_GAP_NS;
    last_observed = now;
    observed = true;
    if (!valid) {
      if (active) {
        failures[0] = failures[1];
        failures[1] = failures[2];
        failures[2] = now;
        failure_count = std::min(failure_count + 1, 3);
        latched = failure_count == 3 && now - failures[0] <= FAILURE_WINDOW_NS;
      }
      active = false;
      warming = false;
      return false;
    }
    if (active) return true;
    if (!warming || gap) healthy_since = now;
    warming = true;
    active = now - healthy_since >= RECOVERY_NS;
    return active;
  }

private:
  uint64_t session = UINT64_MAX, healthy_since = 0, last_observed = 0;
  std::array<uint64_t, 3> failures = {};
  int failure_count = 0;
  bool active = false, warming = false, observed = false, latched = false, clock_invalid = false;
};

struct ModelFovRegion {
  int x = 0, y = 0, w = 0, h = 0;
  bool operator==(const ModelFovRegion &other) const { return x == other.x && y == other.y && w == other.w && h == other.h; }
  bool operator!=(const ModelFovRegion &other) const { return !(*this == other); }
};

// Bounded C3/C3X metering in model coordinates. No image allocation or warp.
// Geometry matches common/transformations/model.py; the PC test checks both.
class ModelFovMeter {
public:
  static constexpr int COLS = 64, ROWS = 36, SAMPLES = COLS * ROWS;
  std::array<float, 8> polygon = {};
  float grey = 0, black_fraction = 0, white_fraction = 0;
  ModelFovStatus status = ModelFovStatus::UNSUPPORTED;

  bool configure(int width, int height, bool wide, const std::array<double, 3> &rpy, int selection, int stride = 0, ModelFovRegion region = {}, int wide_model_down = 0) {
    valid = false;
    count = 0;
    min_x = width; max_x = -1;
    status = ModelFovStatus::UNSUPPORTED;
    if (width != 1928 || height != 1208 || selection < 0 || selection > 4 || wide_model_down < 0 || wide_model_down > 30) return false;
    stride = stride ? stride : width;
    if (stride < width || stride > 8192) return false;
    minimum_bytes = static_cast<size_t>(stride) * (height - 1) + width;
    status = ModelFovStatus::CALIBRATION_INVALID;
    for (double angle : rpy) if (!std::isfinite(angle) || std::abs(angle) > 0.5) return false;
    const double cr = std::cos(rpy[0]), sr = std::sin(rpy[0]);
    const double pitch = rpy[1] - (wide ? wide_model_down * std::acos(-1.0) / 1800.0 : 0.0);
    const double cp = std::cos(pitch), sp = std::sin(pitch);
    const double cy = std::cos(rpy[2]), sy = std::sin(rpy[2]);
    const std::array<double, 9> rotation = {
      cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr,
      sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr,
      -sp, cp * sr, cp * cr,
    };
    const int choices[] = {0, 65, 70, 75, 80};
    const double focal = wide ? (selection ? 256.0 / std::tan(choices[selection] * std::acos(-1.0) / 360.0) : 455.0) : 910.0;
    const double center_y = wide ? (256.0 + 47.6) / 2.0 : 47.6;
    const double camera_focal = wide ? 567.0 : 2648.0;
    auto project = [&](double u, double v, double &x, double &y) {
      const std::array<double, 3> direction = {1.0, (u - 256.0) / focal, (v - center_y) / focal};
      std::array<double, 3> ray = {};
      for (int row = 0; row < 3; ++row) for (int col = 0; col < 3; ++col) ray[row] += rotation[row * 3 + col] * direction[col];
      if (!std::isfinite(ray[0]) || ray[0] <= 1e-6) return false;
      x = camera_focal * ray[1] / ray[0] + width / 2.0;
      y = camera_focal * ray[2] / ray[0] + height / 2.0;
      return std::isfinite(x) && std::isfinite(y);
    };
    scale = camera_focal / focal;
    if (region.w && (region.x < 0 || region.y < 0 || region.w < COLS || region.h < ROWS ||
                     region.w > width || region.h > height || region.x > width - region.w || region.y > height - region.h)) return false;
    const double corners[][2] = {{0, 0}, {511, 0}, {511, 255}, {0, 255}};
    for (int i = 0; i < 4; ++i) {
      double x, y;
      if (!project(corners[i][0], corners[i][1], x, y)) return false;
      if (region.w) { x = region.x + (i == 1 || i == 2 ? region.w - 1 : 0); y = region.y + (i >= 2 ? region.h - 1 : 0); }
      base_polygon[i * 2] = x;
      base_polygon[i * 2 + 1] = y;
    }
    for (int row = 0; row < ROWS; ++row) for (int col = 0; col < COLS; ++col) {
      double x, y;
      if (!project(511.0 * (col + 0.5) / COLS, 255.0 * (row + 0.5) / ROWS, x, y)) return false;
      if (region.w) { x = region.x + (region.w - 1.) * (col + .5) / COLS; y = region.y + (region.h - 1.) * (row + .5) / ROWS; }
      if (x >= 0 && x < width && y >= 0 && y < height) {
        offsets[count] = static_cast<int>(y) * stride + static_cast<int>(x);
        min_x = std::min(min_x, static_cast<int>(x));
        max_x = std::max(max_x, static_cast<int>(x));
        // Fixed model-space prior, not a claim that a pixel is road/vehicle.
        // Every sample retains weight; peripheral objects are never excluded.
        const bool below_horizon = 255.0 * (row + 0.5) / ROWS >= center_y;
        weights[count] = below_horizon ? (col >= COLS / 3 && col < 2 * COLS / 3 ? 3 : 2) : 1;
        regions[count++] = (row * 3 / ROWS) * 3 + col * 3 / COLS;
      }
    }
    // Do not meter a small, clipped fragment as though it were the whole input.
    polygon = base_polygon;
    origin = (base_polygon[0] + base_polygon[2] + base_polygon[4] + base_polygon[6]) * .25f;
    float left = base_polygon[0], right = base_polygon[0];
    for (int i = 1; i < 4; ++i) { left = std::min(left, base_polygon[i * 2]); right = std::max(right, base_polygon[i * 2]); }
    // Preserve zero as the original position, including any admitted original
    // clipping. Translation must not worsen clipping or touch row padding.
    min_shift = std::max(-min_x, -static_cast<int>(std::floor(std::clamp(left, 0.f, float(width)))));
    max_shift = std::min(width - 1 - max_x, static_cast<int>(std::floor(std::clamp(width - 1 - right, 0.f, float(width)))));
    valid = count * 10 >= SAMPLES * 9;
    status = valid ? ModelFovStatus::ACTIVE : ModelFovStatus::REGION_CLIPPED;
    return valid;
  }

  bool measure(const uint8_t *pixels, size_t bytes, bool assisted = false,
               const ExposureBoxTarget *box = nullptr, uint64_t now = 0) {
    desired_bias = 0;
    motion.update(box, now, origin, scale);
    status = ModelFovStatus::INVALID_BUFFER;
    if (!valid || pixels == nullptr || bytes < minimum_bytes) return false;
    motion.offset = std::clamp(motion.offset, float(min_shift), float(max_shift));  // no windup against the image edge
    const int shift = std::clamp(static_cast<int>(std::lround(motion.offset)), min_shift, max_shift);
    polygon = base_polygon;
    for (int corner = 0; corner < 4; ++corner) polygon[corner * 2] += shift;
    std::array<uint32_t, 256> histogram = {}, weighted = {}, moved = {};
    std::array<int, 9> region_count = {}, region_black = {}, region_white = {}, moved_white = {};
    int total_weight = 0, black = 0, white = 0;
    for (int i = 0; i < count; ++i) {
      const uint8_t value = pixels[offsets[i]];
      const uint8_t other = shift ? pixels[static_cast<int64_t>(offsets[i]) + shift] : value;
      ++histogram[value];
      black += other <= 5;
      white += other >= 250;
      if (assisted || shift) {
        const int weight = assisted ? weights[i] : 1;
        weighted[value] += weight;
        total_weight += weight;
        ++region_count[regions[i]];
        region_black[regions[i]] += value <= 5;
        region_white[regions[i]] += value >= 250;
        moved_white[regions[i]] += other >= 250;
        if (shift) moved[other] += weight;
      }
    }
    int median = 255, accumulated = 0;
    for (; median >= 0; --median) {
      accumulated += histogram[median];
      if (accumulated >= count / 2) break;
    }
    grey = median / 256.0f;
    black_fraction = static_cast<float>(black) / count;
    white_fraction = static_cast<float>(white) / count;
    const bool measured = grey > 0 && std::isfinite(grey);
    int weighted_median = median;
    float shifted_highlight = 0;
    if (measured && (assisted || shift)) {
      weighted_median = 255;
      int mass = 0;
      for (; weighted_median > 0; --weighted_median) {
        mass += weighted[weighted_median];
        if (mass >= (assisted ? (total_weight + 1) / 2 : count / 2)) break;
      }
      float shadow = 0, highlight = 0;
      for (int region = 0; region < 9; ++region) {
        if (region_count[region] < SAMPLES / 18) continue;
        highlight = std::max(highlight, float(region_white[region]) / region_count[region]);
        shifted_highlight = std::max(shifted_highlight, float(moved_white[region]) / region_count[region]);
        if (region >= 6) shadow = std::max(shadow, float(region_black[region]) / region_count[region]);
      }
      if (assisted) {
        const float road_bias = std::clamp(std::log2(float(median) / std::max(weighted_median, 1)), -.25f, .25f);
        const float highlight_bias = .125f * std::clamp((highlight - .10f) / .30f, 0.f, 1.f);
        const float shadow_bias = highlight <= .10f ? .125f * std::clamp((shadow - .25f) / .50f, 0.f, 1.f) : 0;
        desired_bias = std::clamp(road_bias + shadow_bias - highlight_bias, -.375f, .375f);
        if (highlight > .10f) desired_bias = std::min(desired_bias, -highlight_bias);
      }
    }
    if (measured && shift) {
      int moved_median = 255, mass = 0;
      for (; moved_median > 0; --moved_median) {
        mass += moved[moved_median];
        if (mass >= (assisted ? (total_weight + 1) / 2 : count / 2)) break;
      }
      // The whole fixed-size translated box is sampled. Never favor lane-line
      // pixels or select a darker patch as the location. A dark shifted scene
      // cannot request more than the existing bounded exposure correction.
      float extra = moved_median > 0 ? std::clamp(std::log2(float(weighted_median) / moved_median), -.125f, .125f) : 0;
      if (shifted_highlight > .10f) extra = std::min(extra, 0.f);
      desired_bias = std::clamp(desired_bias + extra, -.375f, .375f);
    }
    status = measured ? ModelFovStatus::ACTIVE : ModelFovStatus::INVALID_BRIGHTNESS;
    return measured;
  }

  float assisted_grey(bool active, uint64_t now, float base_grey = -1) {
    const float base = base_grey >= 0 ? base_grey : grey;
    if (!active || !last_assisted || now <= last_assisted || now - last_assisted > 250000000ULL) {
      bias = 0;
      last_assisted = active ? now : 0;
      return base;
    }
    const float dt = std::min(float(now - last_assisted) * 1e-9f, 0.1f);
    last_assisted = now;
    bias += std::clamp((desired_bias - bias) * dt / (0.5f + dt), -0.4f * dt, 0.4f * dt);
    return std::min(base * std::exp2(-bias), 1.0f);
  }

  const ExposureBoxMotion &box_state() const { return motion; }

private:
  bool valid = false;
  int count = 0;
  size_t minimum_bytes = 0;
  std::array<uint32_t, SAMPLES> offsets = {};
  std::array<float, 8> base_polygon = {};
  int min_x = 0, max_x = 0, min_shift = 0, max_shift = 0;
  float origin = 0, scale = 1;
  std::array<uint8_t, SAMPLES> weights = {}, regions = {};
  float desired_bias = 0, bias = 0;
  uint64_t last_assisted = 0;
  ExposureBoxMotion motion;
};
