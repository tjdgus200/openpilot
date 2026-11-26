# cython: language_level=3
# distutils: language=c++
"""In-place Y75 processing of the model's packed post-warp camera crops."""
from libc.math cimport nearbyint, pow
from libc.stdint cimport uint8_t, uint32_t
from libc.stdlib cimport abs
from libc.string cimport memcpy, memset


cdef extern from *:
  """
  #include <atomic>
  static inline void daylight75_store_fence() {
  #if defined(__aarch64__)
    // Complete writes to the shared KGSL mapping before the policy GPU submission.
    __asm__ volatile("dsb sy" ::: "memory");
  #else
    std::atomic_thread_fence(std::memory_order_seq_cst);
  #endif
  }
  """
  void daylight75_store_fence() noexcept


def fence_daylight75_writes():
  daylight75_store_fence()


cdef enum:
  BLACK_Y = 16
  HIGHLIGHT_Y = 160
  LEVELS = HIGHLIGHT_Y - BLACK_Y
  TARGET_Y = 75
  PACKED_WIDTH = 256
  PACKED_HEIGHT = 128
  PLANE_SIZE = PACKED_WIDTH * PACKED_HEIGHT
  LUMA_SIZE = 4 * PLANE_SIZE


cdef uint8_t curve_value(int level, double exponent) noexcept nogil:
  return <uint8_t>nearbyint(BLACK_Y + LEVELS * pow(<double>level / LEVELS, exponent))


cdef uint32_t weighted_curve(const uint32_t* histogram, const uint8_t* used, int bins,
                             double exponent) noexcept nogil:
  cdef int index, level
  cdef uint32_t total = 0
  for index in range(bins):
    level = used[index]
    total += histogram[level] * curve_value(level, exponent)
  return total


cdef double apply_crop(uint8_t* crop, bint streaming_mapping) noexcept nogil:
  cdef uint32_t histogram[LEVELS]
  cdef uint8_t used[LEVELS]
  cdef uint8_t table[256]
  cdef uint8_t block[PACKED_WIDTH]
  cdef uint8_t* sample_row
  cdef uint32_t count = 0
  cdef uint32_t total = 0
  cdef uint32_t aim, trial_sum, lower_sum, upper_sum
  cdef int lower_error, upper_error
  cdef int col, row, level, index, value, bins = 0, iteration
  cdef double lower = 0.02, upper = 1.0, mid, exponent

  memset(histogram, 0, sizeof(histogram))
  # Channel 0 is Y(2y, 2x). Every second packed row and column
  # reproduces the reference's 4x4 subsampling on the 512x256 crop.
  for row in range(0, PACKED_HEIGHT, 2):
    sample_row = crop + row * PACKED_WIDTH
    if streaming_mapping:
      # WC/uncached mappings favor sequential bulk reads. Sample in cached stack memory.
      memcpy(block, sample_row, PACKED_WIDTH)
      sample_row = block
    for col in range(0, PACKED_WIDTH, 2):
      value = sample_row[col]
      if BLACK_Y <= value < HIGHLIGHT_Y:
        histogram[value - BLACK_Y] += 1
        count += 1
        total += value
  aim = TARGET_Y * count
  if total >= aim:
    return 1.0

  for level in range(LEVELS):
    if histogram[level] != 0:
      used[bins] = <uint8_t>level
      bins += 1

  for iteration in range(14):
    mid = (lower + upper) / 2
    trial_sum = weighted_curve(histogram, used, bins, mid)
    if trial_sum > aim:
      lower = mid
    else:
      upper = mid

  lower_sum = weighted_curve(histogram, used, bins, lower)
  upper_sum = weighted_curve(histogram, used, bins, upper)
  lower_error = abs(<int>lower_sum - <int>aim)
  upper_error = abs(<int>upper_sum - <int>aim)
  # Comparing integer sums makes an exact fit/tie independent of BLAS
  # summation order. On an equal error prefer the smaller lift.
  exponent = upper if upper_error <= lower_error else lower

  if exponent == 1.0:
    return 1.0

  for value in range(256):
    table[value] = <uint8_t>value
  for level in range(LEVELS):
    value = curve_value(level, exponent)
    if value > BLACK_Y + level:
      table[BLACK_Y + level] = <uint8_t>value

  if streaming_mapping:
    for row in range(4 * PACKED_HEIGHT):
      index = row * PACKED_WIDTH
      memcpy(block, crop + index, PACKED_WIDTH)
      for col in range(PACKED_WIDTH):
        block[col] = table[block[col]]
      memcpy(crop + index, block, PACKED_WIDTH)
  else:
    for index in range(LUMA_SIZE):
      crop[index] = table[crop[index]]
  return exponent


def apply_daylight75(images, bint road_enabled, bint wide_enabled):
  """Apply Y75 in place without requiring the caller to consume display metadata."""
  apply_daylight75_with_exponents(images, road_enabled, wide_enabled)


def apply_daylight75_with_exponents(images, bint road_enabled, bint wide_enabled, bint streaming_mapping=False):
  """Return the fitted exponents for these exact crops; 1 means identity."""
  cdef double main = 1.0, extra = 1.0
  if not (road_enabled or wide_enabled):
    return (1.0, 1.0)
  cdef uint8_t[:, :, :, ::1] view = images
  if (view.shape[0] != 2 or view.shape[1] != 6 or
      view.shape[2] != PACKED_HEIGHT or view.shape[3] != PACKED_WIDTH):
    raise ValueError("Y75 expects a writable uint8 camera buffer shaped (2, 6, 128, 256)")
  with nogil:
    if road_enabled:
      main = apply_crop(&view[0, 0, 0, 0], streaming_mapping)
    if wide_enabled:
      extra = apply_crop(&view[1, 0, 0, 0], streaming_mapping)
  return main, extra
