"""Compare constant selection with target 7c008e58, including rejected inputs."""
import numpy as np
import pytest

from tinygrad import Tensor
from tinygrad.nn.onnx import get_onnx_ops

GATHER = get_onnx_ops()['Gather']


def legacy_gather(x, indices, axis=0):
  # Exact pre-coalescing constant selection from 7c008e58.
  axis = x._resolve_dim(axis)
  indices_list = [indices] if isinstance(indices, int) else list(indices)
  indices_shape = () if isinstance(indices, int) else (len(indices_list),)
  ret_shape = x.shape[:axis] + indices_shape + x.shape[axis+1:]
  index_consts = [x.shape[axis]+i if i<0 else i for i in indices_list]
  args = [[(0,s) if j != axis else (i,i+1) for j, s in enumerate(x.shape)] for i in index_consts]
  return x.shrink(arg=tuple(args[0])).cat(*[x.shrink(arg=tuple(arg)) for arg in args[1:]], dim=axis).reshape(ret_shape)


@pytest.mark.parametrize('dtype', [np.float16, np.float32, np.int32, np.int64])
@pytest.mark.parametrize('axis', [0, 1, 2, -1, -2, -3])
def test_runs_match_legacy_for_repeated_reversed_negative_and_boundary_indices(dtype, axis):
  shape = [3, 4, 5]
  values = np.arange(np.prod(shape)).reshape(shape).astype(dtype)
  n = shape[axis]
  selections = [[0, 1, 1, 0, -1], list(range(n)), list(range(-n, 0)), [n-1, 0, n-1], -n]
  for indices in selections:
    old = legacy_gather(Tensor(values), indices, axis).numpy()
    new = GATHER(Tensor(values), indices, axis).numpy()
    np.testing.assert_array_equal(new, old)
    np.testing.assert_array_equal(new, np.take(values, indices, axis=axis))


@pytest.mark.parametrize('indices,axis', [([], 0), ([3], 0), ([-4], 0), ([0, 1, 2, 3], 0), ([0], 3), ([0], -4)])
def test_invalid_selection_preserves_existing_rejection(indices, axis):
  values = np.arange(60, dtype=np.float32).reshape(3, 4, 5)
  with pytest.raises(Exception) as original:
    legacy_gather(Tensor(values), indices, axis).numpy()
  with pytest.raises(type(original.value)):
    GATHER(Tensor(values), indices, axis).numpy()


def test_nonfinite_values_are_selected_without_sanitizing():
  values = np.array([np.nan, np.inf, -np.inf, -0., 3.], dtype=np.float32)
  indices = [0, 1, 2, 3, 4, 0, -1]
  old = legacy_gather(Tensor(values), indices).numpy()
  new = GATHER(Tensor(values), indices).numpy()
  np.testing.assert_array_equal(new, old)
  np.testing.assert_array_equal(np.signbit(new), np.signbit(old))


def test_tensor_indices_retain_multidimensional_selection():
  values = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
  indices = np.array([[2, 0], [1, 2]], dtype=np.int32)
  np.testing.assert_array_equal(GATHER(Tensor(values), Tensor(indices), 1).numpy(), np.take(values, indices, axis=1))
