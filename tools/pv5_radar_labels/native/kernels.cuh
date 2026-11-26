#pragma once
#include <cuda_fp16.h>
#include <cuda_runtime.h>
#include <cstdint>
#include "pixels.h"

namespace pv5_yolo {
constexpr int kSide = 640, kRows = 25200, kColumns = 85, kCapacity = 2048;
struct Candidate {
  float xyxy[4], confidence;
  int32_t class_id, anchor;
};
static_assert(sizeof(Candidate) == 28);
struct ResultBuffer { int count; Candidate rows[kCapacity]; };

__global__ void preprocess(const unsigned char *src, int w, int h, int stride, bool six, int rw, int rh,
                           __half *out) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  constexpr int plane = kSide * kSide;
  if (i >= plane) return;
  const RGB p = letterbox_pixel(src, w, h, stride, six, rw, rh, i % kSide, i / kSide);
  // Match RGB24 normalization in the replay. The packed input is converted directly on GPU.
  out[i] = __float2half_rn(floorf(p.r + .5f) / 255.f);
  out[i + plane] = __float2half_rn(floorf(p.g + .5f) / 255.f);
  out[i + 2 * plane] = __float2half_rn(floorf(p.b + .5f) / 255.f);
}

__global__ void decode(const __half *output, ResultBuffer *result) {
  const int anchor = blockIdx.x * blockDim.x + threadIdx.x;
  if (anchor >= kRows) return;
  const __half *row = output + anchor * kColumns;
  const float objectness = __half2float(row[4]);
  if (!(objectness > .1f && objectness <= 1.f)) return;
  int label = -1;
  float best = -1.f;
  for (int c = 0; c < 80; ++c) {
    const float p = __half2float(row[c + 5]);
    if (!isfinite(p) || p < 0.f || p > 1.f) return;
    if (p >= best) { label = c; best = p; }
  }
  if (!(label == 0 || label == 1 || label == 2 || label == 3 || label == 5 || label == 7)) return;
  const float confidence = objectness * best;
  if (confidence <= .1f) return;
  const float x = __half2float(row[0]), y = __half2float(row[1]);
  const float w = __half2float(row[2]), h = __half2float(row[3]);
  if (!isfinite(x) || !isfinite(y) || !isfinite(w) || !isfinite(h) || w <= 0.f || h <= 0.f) return;
  const int slot = atomicAdd(&result->count, 1);
  if (slot < kCapacity) {
    result->rows[slot] = {{x-w/2, y-h/2, x+w/2, y+h/2}, confidence, label, anchor};
  }
}

} // namespace pv5_yolo
