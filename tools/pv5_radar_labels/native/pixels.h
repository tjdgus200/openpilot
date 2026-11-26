#pragma once

// The same pixel math is exercised by the host fixture and the device kernel.
#ifdef __CUDACC__
#define PV5_PIXEL_FN __host__ __device__ inline
#else
#define PV5_PIXEL_FN inline
#endif

struct RGB { float r, g, b; };
PV5_PIXEL_FN float channel(float v) { return v < 0.f ? 0.f : (v > 255.f ? 255.f : v); }

PV5_PIXEL_FN RGB read_pixel(const unsigned char *src, int w, int h, int stride, bool six, int x, int y) {
  if (!six) {
    const unsigned char *p = src + y * stride + 3 * x;
    return {float(p[0]), float(p[1]), float(p[2])};
  }
  // frames_to_tensor() in compile_modeld.py packs current road Y00,Y10,Y01,Y11,U,V.
  const int plane = (w / 2) * (h / 2), i = (y / 2) * (w / 2) + x / 2;
  const float luma = 298.f * (int(src[((x % 2) * 2 + y % 2) * plane + i]) - 16);
  const int u = int(src[4 * plane + i]) - 128, v = int(src[5 * plane + i]) - 128;
  // Same BT.601 limited-range convention as the road-camera RGB helper.
  return {channel((luma + 409 * v) / 256.f), channel((luma - 100 * u - 208 * v) / 256.f),
          channel((luma + 516 * u) / 256.f)};
}

PV5_PIXEL_FN RGB mix_rgb(RGB a, RGB b, float t) {
  return {a.r + t * (b.r - a.r), a.g + t * (b.g - a.g), a.b + t * (b.b - a.b)};
}

PV5_PIXEL_FN RGB letterbox_pixel(const unsigned char *src, int w, int h, int stride, bool six,
                                 int rw, int rh, int x, int y) {
  const int dx = x - (640 - rw) / 2, dy = y - (640 - rh) / 2;
  if (dx < 0 || dy < 0 || dx >= rw || dy >= rh) return {114.f, 114.f, 114.f};
  const float sx = (dx + 0.5f) * w / rw - 0.5f, sy = (dy + 0.5f) * h / rh - 0.5f;
  const float px = sx < 0 ? 0.f : (sx > w - 1 ? float(w - 1) : sx);
  const float py = sy < 0 ? 0.f : (sy > h - 1 ? float(h - 1) : sy);
  const int x0 = int(px), y0 = int(py);
  const int x1 = x0 < w - 1 ? x0 + 1 : x0, y1 = y0 < h - 1 ? y0 + 1 : y0;
  const RGB upper = mix_rgb(read_pixel(src,w,h,stride,six,x0,y0), read_pixel(src,w,h,stride,six,x1,y0), px - x0);
  const RGB lower = mix_rgb(read_pixel(src,w,h,stride,six,x0,y1), read_pixel(src,w,h,stride,six,x1,y1), px - x0);
  return mix_rgb(upper, lower, py - y0);
}
