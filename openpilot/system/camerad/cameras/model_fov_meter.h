#pragma once

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>

struct ModelFovSnapshot {
  bool ready = false;
  int selection = 0;
  uint64_t generation = 0, valid_until = 0;
  std::array<double, 3> rpy = {};
};

// One writer, one reader. One bounded read attempt, never spin behind the worker.
// Atomic fields avoid data races even when a read overlaps a publication.
class ModelFovMailbox {
public:
  ModelFovMailbox() { for (auto &angle : rpy) angle.store(0.0); }
  void publish(const ModelFovSnapshot &value) {
    ++version;
    ready.store(value.ready);
    selection.store(value.selection);
    generation.store(value.generation);
    valid_until.store(value.valid_until);
    for (int i = 0; i < 3; ++i) rpy[i].store(value.rpy[i]);
    ++version;
  }
  bool read(ModelFovSnapshot &value) const {
    const auto before = version.load();
    if (before & 1) return false;
    ModelFovSnapshot next;
    next.ready = ready.load();
    next.selection = selection.load();
    next.generation = generation.load();
    next.valid_until = valid_until.load();
    for (int i = 0; i < 3; ++i) next.rpy[i] = rpy[i].load();
    if (before != version.load()) return false;
    value = next;
    return true;
  }
private:
  static_assert(std::atomic<double>::is_always_lock_free && std::atomic<uint64_t>::is_always_lock_free);
  std::atomic<uint64_t> version{0}, generation{0}, valid_until{0};
  std::atomic<bool> ready{false};
  std::atomic<int> selection{0};
  std::array<std::atomic<double>, 3> rpy{};
};

// Bounded C3/C3X metering in model coordinates. No image allocation or warp.
// Geometry matches common/transformations/model.py; the PC test checks both.
class ModelFovMeter {
public:
  static constexpr int COLS = 64, ROWS = 36, SAMPLES = COLS * ROWS;
  std::array<float, 8> polygon = {};
  float grey = 0, black_fraction = 0, white_fraction = 0;

  bool configure(int width, int height, bool wide, const std::array<double, 3> &rpy, int selection, int stride = 0) {
    valid = false;
    count = 0;
    if (width != 1928 || height != 1208 || selection < 0 || selection > 4) return false;
    stride = stride ? stride : width;
    if (stride < width || stride > 8192) return false;
    minimum_bytes = static_cast<size_t>(stride) * (height - 1) + width;
    for (double angle : rpy) if (!std::isfinite(angle) || std::abs(angle) > 0.5) return false;
    const double cr = std::cos(rpy[0]), sr = std::sin(rpy[0]);
    const double cp = std::cos(rpy[1]), sp = std::sin(rpy[1]);
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
    const double corners[][2] = {{0, 0}, {511, 0}, {511, 255}, {0, 255}};
    for (int i = 0; i < 4; ++i) {
      double x, y;
      if (!project(corners[i][0], corners[i][1], x, y)) return false;
      polygon[i * 2] = x;
      polygon[i * 2 + 1] = y;
    }
    for (int row = 0; row < ROWS; ++row) for (int col = 0; col < COLS; ++col) {
      double x, y;
      if (!project(511.0 * (col + 0.5) / COLS, 255.0 * (row + 0.5) / ROWS, x, y)) return false;
      if (x >= 0 && x < width && y >= 0 && y < height) offsets[count++] = static_cast<int>(y) * stride + static_cast<int>(x);
    }
    // Do not meter a small, clipped fragment as though it were the whole input.
    valid = count * 10 >= SAMPLES * 9;
    return valid;
  }

  bool measure(const uint8_t *pixels, size_t bytes) {
    if (!valid || pixels == nullptr || bytes < minimum_bytes) return false;
    std::array<uint32_t, 256> histogram = {};
    int black = 0, white = 0;
    for (int i = 0; i < count; ++i) {
      const uint8_t value = pixels[offsets[i]];
      ++histogram[value];
      black += value <= 5;
      white += value >= 250;
    }
    int median = 255, accumulated = 0;
    for (; median >= 0; --median) {
      accumulated += histogram[median];
      if (accumulated >= count / 2) break;
    }
    grey = median / 256.0f;
    black_fraction = static_cast<float>(black) / count;
    white_fraction = static_cast<float>(white) / count;
    return grey > 0 && std::isfinite(grey);
  }

private:
  bool valid = false;
  int count = 0;
  size_t minimum_bytes = 0;
  std::array<uint32_t, SAMPLES> offsets = {};
};
