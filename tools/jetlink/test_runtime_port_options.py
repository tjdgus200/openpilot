import json
import pytest
from runtime_port_options import apply, KEYS


def test_absent_options_leave_environment_unchanged(tmp_path):
  env = {}
  assert apply(tmp_path/'absent', env) == {}
  assert env == {}


def test_release_options_and_explicit_off(tmp_path):
  path = tmp_path/'options.json'
  path.write_text(json.dumps(dict.fromkeys(KEYS, True)))
  env = {'CARROT_TRT_DEVICE_STATE': '0'}
  assert apply(path, env) == {'CARROT_TRT_DEVICE_STATE': '0', 'CARROT_JETLINK_AIO_WRITE': '1'}


@pytest.mark.parametrize('value', [{}, {'unexpected': True}, dict.fromkeys(KEYS, 1)])
def test_invalid_options_fail_before_partial_change(tmp_path, value):
  path = tmp_path/'options.json'
  path.write_text(json.dumps(value))
  env = {}
  with pytest.raises(ValueError):
    apply(path, env)
  assert env == {}
