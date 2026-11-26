"""Verify the model crop contract and separation of camera controls."""
import numpy as np
import pytest

from openpilot.selfdrive.modeld.daylight_luma import apply_daylight75


@pytest.mark.parametrize(("road", "wide"), [(False, False), (True, False), (False, True), (True, True)])
def test_separate_crops_and_protected_values(road, wide):
  crops = np.empty((2, 6, 128, 256), dtype=np.uint8)
  crops[0, :4] = 35
  crops[1, :4] = 61
  for lens in range(2):
    crops[lens, 1, 0, :6] = [0, 15, 16, 160, 200, 255]
    crops[lens, 4:] = np.arange(256, dtype=np.uint8)
  before = crops.copy()

  assert apply_daylight75(crops, road, wide) is None

  for lens, enabled in enumerate((road, wide)):
    assert crops[lens, 0, 0, 0] == (75 if enabled else before[lens, 0, 0, 0])
    np.testing.assert_array_equal(crops[lens, 1, 0, :6], before[lens, 1, 0, :6])
    np.testing.assert_array_equal(crops[lens, 4:], before[lens, 4:])
    if not enabled:
      np.testing.assert_array_equal(crops[lens], before[lens])


def test_crop_fit_uses_even_rows_and_columns_of_packed_y00():
  crops = np.full((2, 6, 128, 256), 139, dtype=np.uint8)
  crops[0, 0, ::2, ::2] = 40

  apply_daylight75(crops, True, False)

  assert np.all(crops[0, 0, ::2, ::2] == 75)
  assert np.all(crops[1] == 139)


@pytest.mark.parametrize("level", [0, 16, 75, 100, 159, 160, 255])
def test_no_lift_when_measured_mean_already_at_target_or_black_is_fixed(level):
  crops = np.full((2, 6, 128, 256), level, dtype=np.uint8)
  apply_daylight75(crops, True, True)
  assert np.all(crops == level)


def test_disabled_ignores_buffer_and_enabled_checks_shape():
  assert apply_daylight75(None, False, False) is None
  with pytest.raises(ValueError, match="shaped"):
    apply_daylight75(np.empty((2, 6, 128, 128), dtype=np.uint8), True, False)
  with pytest.raises(ValueError):
    apply_daylight75(np.empty((2, 6, 128, 512), dtype=np.uint8)[..., ::2], True, False)
  crops = np.empty((2, 6, 128, 256), dtype=np.uint8)
  crops.flags.writeable = False
  with pytest.raises(ValueError):
    apply_daylight75(crops, False, True)
