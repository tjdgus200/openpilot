#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

// Lane geometry locates a whole metering box. Pixel darkness never locates it.
struct ExposureLaneInput {
  struct Point { float x = 0, y = 0, z = 0; };
  std::array<Point, 33> left = {}, right = {};
  int count = 0;
  std::array<float, 2> probability = {}, deviation = {};
};

struct ExposureBoxTarget {
  bool valid = false;
  uint64_t valid_until = 0;
  float center_x = 0;  // absolute source-image coordinate; no vertical motion
};

inline ExposureBoxTarget exposure_box_target(const ExposureLaneInput &in, bool wide, int selection,
                                             const std::array<double, 3> &rpy) {
  ExposureBoxTarget out;
  if (in.count < 6 || in.count > 33 || selection < 0 || selection > 4) return out;
  for (int side = 0; side < 2; ++side) {
    if (!std::isfinite(in.probability[side]) || in.probability[side] < .8f || in.probability[side] > 1 ||
        !std::isfinite(in.deviation[side]) || in.deviation[side] < 0 || in.deviation[side] > .3f) return out;
  }
  for (double angle : rpy) if (!std::isfinite(angle) || std::abs(angle) > .5) return out;
  for (int i = 0; i < in.count; ++i) {
    const auto &l = in.left[i], &r = in.right[i];
    if (!std::isfinite(l.x) || !std::isfinite(r.x) || l.x < 0 || r.x > 200 || std::abs(l.x - r.x) > .01f ||
        (i && (l.x <= in.left[i - 1].x || r.x <= in.right[i - 1].x))) return out;
    if (l.x < 4 || l.x > 60) continue;
    for (const auto &p : {l, r}) {
      if (!std::isfinite(p.y) || !std::isfinite(p.z) || std::abs(p.y) > 30 || p.z < .1f || p.z > 5) return out;
    }
    if (r.y - l.y < 2.2f || r.y - l.y > 5.5f) return out;
  }
  const double cr = std::cos(rpy[0]), sr = std::sin(rpy[0]);
  const double cp = std::cos(rpy[1]), sp = std::sin(rpy[1]);
  const double cy = std::cos(rpy[2]), sy = std::sin(rpy[2]);
  const std::array<double, 3> forward = {cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr};
  const std::array<double, 3> lateral = {sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr};
  double sum = 0;
  for (double lookahead : {15., 25., 35.}) {
    bool found = false;
    for (int i = 1; i < in.count; ++i) {
      if (in.left[i - 1].x < 4 || in.left[i].x > 60 ||
          in.left[i - 1].x > lookahead || in.left[i].x < lookahead) continue;
      const double f = (lookahead - in.left[i - 1].x) / (in.left[i].x - in.left[i - 1].x);
      const auto center = [&](bool z) {
        const double before = z ? (in.left[i - 1].z + in.right[i - 1].z) * .5 : (in.left[i - 1].y + in.right[i - 1].y) * .5;
        const double after = z ? (in.left[i].z + in.right[i].z) * .5 : (in.left[i].y + in.right[i].y) * .5;
        return before + f * (after - before);
      };
      const std::array<double, 3> point = {lookahead, center(false), center(true)};
      double x = 0, y = 0;
      for (int axis = 0; axis < 3; ++axis) { x += forward[axis] * point[axis]; y += lateral[axis] * point[axis]; }
      if (!std::isfinite(x) || !std::isfinite(y) || x <= 0) return out;
      sum += 964 + (wide ? 567 : 2648) * y / x;
      found = true;
      break;
    }
    if (!found) return out;
  }
  out.center_x = sum / 3;
  // A valid geometric target may lie beyond the current image. The complete
  // box stops at the image boundary in ModelFovMeter rather than recentering.
  out.valid = std::isfinite(out.center_x);
  return out;
}

class ExposureBoxMotion {
public:
  float offset = 0;

  void update(const ExposureBoxTarget *input, uint64_t now, float origin, float scale) {
    if (!input) { if (last) *this = {}; return; }
    if (!last || now <= last || now - last > 250000000ULL) { *this = {}; last = now; return; }
    const float dt = std::min(float(now - last) * 1e-9f, .1f);
    last = now;
    const bool valid = input->valid && now < input->valid_until && std::isfinite(input->center_x);
    if (!valid) healthy_since = 0;
    else if (!healthy_since) healthy_since = now;
    const float goal = valid && now - healthy_since >= 500000000ULL ?
      input->center_x - origin : 0;
    offset += std::clamp(goal - offset, -64.f * scale * dt, 64.f * scale * dt);
  }

private:
  uint64_t last = 0, healthy_since = 0;
};
