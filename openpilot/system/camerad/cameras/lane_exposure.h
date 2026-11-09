#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

// Advisory metering only. Never changes camera/model transforms or control.
struct ExposureLaneInput {
  struct Point { float x = 0, y = 0, z = 0; };
  std::array<Point, 33> left = {}, right = {};
  int count = 0;
  std::array<float, 2> probability = {}, deviation = {};
};

struct ExposureLaneCorridor {
  static constexpr int ROWS = 36;
  bool valid = false;
  uint64_t valid_until = 0;
  std::array<float, ROWS> left = {}, right = {};  // model pixels; zero width means omitted row
};

inline ExposureLaneCorridor exposure_lane_corridor(const ExposureLaneInput &in, bool wide, int selection) {
  ExposureLaneCorridor out;
  if (in.count < 6 || in.count > 33 || selection < 0 || selection > 4) return out;
  for (int i = 0; i < 2; ++i) {
    if (!std::isfinite(in.probability[i]) || in.probability[i] < .8f || in.probability[i] > 1 ||
        !std::isfinite(in.deviation[i]) || in.deviation[i] < 0 || in.deviation[i] > .3f) return out;
  }
  const int choices[] = {0, 65, 70, 75, 80};
  const double focal = wide ? (selection ? 256.0 / std::tan(choices[selection] * std::acos(-1.0) / 360.0) : 455.0) : 910.0;
  const double cy = wide ? (256.0 + 47.6) / 2.0 : 47.6;
  std::array<std::array<float, 33>, 2> us = {}, vs = {};
  int count = 0;
  for (int i = 0; i < in.count; ++i) {
    const auto &l = in.left[i], &r = in.right[i];
    if (!std::isfinite(l.x) || !std::isfinite(r.x) || l.x < 0 || r.x > 200 || std::abs(l.x - r.x) > .01f ||
        (i && (l.x <= in.left[i - 1].x || r.x <= in.right[i - 1].x))) return out;
    if (l.x < 4 || l.x > 60) continue;
    for (const auto &p : {l, r}) {
      if (!std::isfinite(p.y) || !std::isfinite(p.z) || std::abs(p.y) > 30 || p.z < .1f || p.z > 5) return out;
    }
    if (r.y - l.y < 2.2f || r.y - l.y > 5.5f) return out;
    for (int side = 0; side < 2; ++side) {
      const auto &p = side ? r : l;
      us[side][count] = 256 + focal * p.y / p.x;
      vs[side][count] = cy + focal * p.z / p.x;
      // A folded or near-horizontal image curve cannot define one corridor per row.
      if (count && vs[side][count] >= vs[side][count - 1] - .01f) return out;
    }
    ++count;
  }
  if (count < 6) return out;
  int rows = 0;
  for (int row = 0; row < ExposureLaneCorridor::ROWS; ++row) {
    const float v = 255.f * (row + .5f) / ExposureLaneCorridor::ROWS;
    std::array<float, 2> edge = {};
    bool both = true;
    for (int side = 0; side < 2; ++side) {
      bool found = false;
      for (int i = 1; i < count; ++i) {
        if (vs[side][i] <= v && v <= vs[side][i - 1]) {
          const float f = (v - vs[side][i]) / (vs[side][i - 1] - vs[side][i]);
          edge[side] = us[side][i] + f * (us[side][i - 1] - us[side][i]);
          found = true;
          break;
        }
      }
      both &= found;
    }
    const float l = std::max(edge[0], 0.f), r = std::min(edge[1], 511.f);
    if (both && r - l >= 16 && r - l <= 450) {
      out.left[row] = l;
      out.right[row] = r;
      ++rows;
    }
  }
  out.valid = rows >= 6;
  return out;
}

class ExposureLaneWeights {
public:
  // Model-space edges; useful for read-only offline rendering and bounds tests.
  std::array<float, 36> left = {}, right = {}, strength = {};

  void update(const ExposureLaneCorridor *input, uint64_t now) {
    if (!input) { if (last) *this = {}; return; }  // Explicit user OFF restores the chosen base.
    if (!last || now <= last || now - last > 250000000ULL) {
      *this = {};
      last = now;
      return;
    }
    const float dt = std::min(float(now - last) * 1e-9f, .1f);
    last = now;
    const bool valid = input->valid && now < input->valid_until;
    if (!valid) healthy_since = 0;
    else if (!healthy_since) healthy_since = now;
    const bool qualified = valid && now - healthy_since >= 500000000ULL;
    for (int row = 0; row < 36; ++row) {
      const bool row_valid = qualified && std::isfinite(input->left[row]) && std::isfinite(input->right[row]) &&
        input->left[row] >= 0 && input->right[row] <= 511 && input->right[row] - input->left[row] >= 16;
      if (row_valid) {
        if (strength[row] == 0) { left[row] = input->left[row]; right[row] = input->right[row]; }
        else {
          // Limits also apply when confident lane predictions suddenly jump.
          left[row] += std::clamp(input->left[row] - left[row], -64.f * dt, 64.f * dt);
          right[row] += std::clamp(input->right[row] - right[row], -64.f * dt, 64.f * dt);
        }
      }
      strength[row] += std::clamp((row_valid ? 1.f : 0.f) - strength[row], -dt, dt);
    }
  }

  int extra(int cell) const {
    const int row = cell / 64, col = cell % 64;
    if (strength[row] <= 0 || right[row] <= left[row]) return 0;
    const float u = 511.f * (col + .5f) / 64;
    const float center = (left[row] + right[row]) * .5f, half = (right[row] - left[row]) * .5f;
    // Soft-edged extra weight; base samples everywhere remain represented.
    return std::lround(16 * strength[row] * std::clamp((half - std::abs(u - center)) / (half * .25f), 0.f, 1.f));
  }

  bool active() const { return std::any_of(strength.begin(), strength.end(), [](float value) { return value > 0; }); }

private:
  uint64_t last = 0, healthy_since = 0;
};
