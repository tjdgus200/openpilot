import json

import pytest

import runtime_port_options as options


@pytest.mark.parametrize('c3,expected', [(True, '1'), (False, '0')])
def test_bundled_aio_default_is_limited_to_tested_c3(monkeypatch, c3, expected):
  monkeypatch.setattr(options, 'c3_aio_supported', lambda: c3)
  env = {}
  options.apply(environ=env)
  assert env == {'CARROT_TRT_DEVICE_STATE': '1', 'CARROT_JETLINK_AIO_WRITE': expected}


def test_process_can_explicitly_disable_ported_optimizations(monkeypatch):
  monkeypatch.setattr(options, 'c3_aio_supported', lambda: True)
  env = dict.fromkeys(options.KEYS, '0')
  assert options.apply(environ=env) == env == dict.fromkeys(options.KEYS, '0')


def test_missing_or_malformed_release_choices_never_silently_enable(tmp_path):
  path = tmp_path / 'options.json'
  assert options.apply(path, {}) == {}
  path.write_text(json.dumps({'CARROT_TRT_DEVICE_STATE': True}))
  with pytest.raises(ValueError):
    options.apply(path, {})
