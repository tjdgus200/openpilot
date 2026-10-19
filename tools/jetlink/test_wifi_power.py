from unittest.mock import Mock

import pytest

import wifi_power as policy


def test_policy_disables_wifi_only_without_link_bounce_and_avoids_profile_rewrites(tmp_path, monkeypatch):
  monkeypatch.setattr(policy.os, 'geteuid', lambda: 0)
  uid = '12345678-1234-1234-1234-123456789abc'
  calls, value = [], ['default']
  def command(*args):
    calls.append(args)
    if args[-1] == 'show':
      return uid + ':802-11-wireless\n22345678-1234-1234-1234-123456789abc:802-3-ethernet'
    if args[:2] == ('-g', '802-11-wireless.powersave'):
      return value[0]
    if args[:2] == ('connection', 'modify'):
      value[0] = 'disable'
    return ''
  wireless = Mock()
  wireless.interfaces.return_value = [{'index': 1, 'type': 2}, {'index': 2, 'type': 3}, {'index': 3, 'type': 10}]
  wireless.power_save.return_value = 1
  config = tmp_path/'awake.conf'
  assert policy.configure(command, wireless, config) == 1
  assert config.read_bytes() == policy.CONFIG
  assert [c.args for c in wireless.set_power_save.call_args_list] == [(1, 0), (2, 0)]
  before = config.stat().st_mtime_ns
  assert policy.configure(command, wireless, config) == 0
  assert config.stat().st_mtime_ns == before
  assert sum(c[:2] == ('connection', 'modify') for c in calls) == 1
  assert not any('up' in c or 'down' in c or 'reapply' in c or 'restart' in c for c in calls)
  wireless.close.assert_not_called()


def test_policy_requires_root_and_refuses_symlink_config(tmp_path, monkeypatch):
  command = Mock()
  monkeypatch.setattr(policy.os, 'geteuid', lambda: 1000)
  with pytest.raises(PermissionError):
    policy.configure(command, Mock(), tmp_path/'awake.conf')
  command.assert_not_called()
  monkeypatch.setattr(policy.os, 'geteuid', lambda: 0)
  target = tmp_path/'target'
  target.write_text('untouched')
  link = tmp_path/'awake.conf'
  link.symlink_to(target)
  with pytest.raises(ValueError):
    policy.configure(command, Mock(), link)
  assert target.read_text() == 'untouched'
