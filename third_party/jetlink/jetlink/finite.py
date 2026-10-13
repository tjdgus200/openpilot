"""Exact, reusable finite-value checking for large native float16 state arrays.

Binary16 has a non-finite value exactly when all five exponent bits are set.
This avoids scalar half-to-float conversions on CPUs without a fast NumPy
float16 isfinite loop. This is a full scan, not sampling or range clipping.
"""
import numpy as np


class FiniteChecker:
  def __init__(self, capacity: int):
    self.bits = np.empty(capacity, dtype=np.uint16)
    self.valid = np.empty(capacity, dtype=np.bool_)

  def __call__(self, value: np.ndarray) -> bool:
    if (value.dtype != np.dtype(np.float16) or not value.flags.c_contiguous
        or value.size > self.bits.size):
      return bool(np.isfinite(value).all())
    bits = self.bits[:value.size]
    valid = self.valid[:value.size]
    np.bitwise_and(value.view(np.uint16).reshape(-1), 0x7c00, out=bits)
    np.not_equal(bits, 0x7c00, out=valid)
    return bool(valid.all())
