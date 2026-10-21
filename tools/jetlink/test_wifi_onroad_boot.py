"""Onroad boot/recovery without a phone; synthetic NM, no RF/vehicle claim."""
import json
import os
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock  # noqa: TID251 -- isolate privileged OS/network operations

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux AP worker')

import wifi_direct as direct
import wifi_publisher as publisher
from wifi_protocol import HEADER


@pytest.fixture
def ap_setup(tmp_path, monkeypatch):
  value = {'uuid': '12345678-1234-1234-1234-123456789abc',
           'ssid': 'Carrot-Jetson-12345678', 'password': 'synthetic AP test password',
           'subnet': '172.29.77.0/24'}
  wireless = Mock()
  wireless.create.return_value = 4
  wireless.interfaces.return_value = [{'name': 'wljetson0', 'index': 4}]
  wireless.power_save.return_value = 1
  calls = []
  active = set()
  def nm(*args):
    calls.append(args)
    if args[:3] == ('-t', '-f', 'IN-USE,FREQ'):
      return ''  # No phone hotspot association or saved scan results.
    if args[:3] == ('-g', 'GENERAL.STATE', 'device'):
      return '30 (disconnected)'
    if args[-1] == '--active':
      return '\n'.join(active)
    if args[:4] == ('--wait', '12', 'connection', 'up'):
      active.add(value['uuid'])
    if args[:4] == ('--wait', '3', 'connection', 'down'):
      active.clear()
    return ''
  monkeypatch.setattr(direct, 'offroad', lambda: False)
  monkeypatch.setattr(direct, 'configuration', lambda: dict(value))
  monkeypatch.setattr(direct, 'nm', nm)
  monkeypatch.setattr(direct, 'PROFILE', tmp_path / 'direct.nmconnection')
  monkeypatch.setattr(direct.subprocess, 'check_output', lambda *a, **k: '[]')
  monkeypatch.setattr(direct, 'Sharing', Mock)
  # Preserve the real cached-only channel selector with a controlled NM query.
  ap = direct.AccessPoint(wireless, command=nm)
  return NS(ap=ap, wireless=wireless, calls=calls, value=value, active=active)


def test_onroad_boot_without_hotspot_creates_owned_ap_without_touching_usb_or_uplink(ap_setup):
  s = ap_setup
  assert s.ap.start() is True and s.ap.active()
  assert s.ap.stage == 'active'
  s.wireless.create.assert_called_once_with()
  s.wireless.set_power_save.assert_called_once_with(4, 0)
  assert ('--wait', '12', 'connection', 'up', 'uuid', s.value['uuid'], 'ifname', 'wljetson0') in s.calls
  scan = [c for c in s.calls if 'wifi' in c and 'list' in c]
  assert scan == [('-t', '-f', 'IN-USE,FREQ', 'device', 'wifi', 'list', 'ifname', 'wlan0', '--rescan', 'no')]
  # No Internet/NAT correction is run onroad. No USB reset, station disconnect,
  # radio toggle or profile modification is issued by AP activation.
  s.ap.sharing.ensure.assert_not_called()
  assert not any('radio' in c or 'disconnect' in c or 'modify' in c for c in s.calls)
  assert all('wlan0' not in c for c in s.calls if c not in scan)
  text = direct.PROFILE.read_text()
  assert 'band=a\nchannel=36\n' in text
  assert 'interface-name=wljetson0' in text and 'never-default=true' in text


def test_root_worker_retries_onroad_and_does_not_recreate_healthy_ap(ap_setup, tmp_path, monkeypatch):
  s = ap_setup
  clock = [0.]
  class Stop:
    def is_set(self):
      return False
    def set(self):
      pass
    def wait(self, seconds):
      clock[0] += seconds
  attempts = []
  def create():
    attempts.append(clock[0])
    if len(attempts) == 1:
      raise OSError('NM/radio not yet ready')
    return 4
  s.wireless.create.side_effect = create
  monkeypatch.setattr(direct.os, 'geteuid', lambda: 0)
  monkeypatch.setattr(direct, 'LOCK', tmp_path / 'lock')
  monkeypatch.setattr(direct, 'STATUS', tmp_path / 'status')
  monkeypatch.setattr(direct, 'low_priority', Mock())
  monkeypatch.setattr(direct, 'Wireless', lambda: s.wireless)
  monkeypatch.setattr(direct, 'AccessPoint', lambda *_: s.ap)
  monkeypatch.setattr(direct.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(direct.threading, 'Event', Stop)
  monkeypatch.setattr(direct.signal, 'signal', lambda *a: None)
  states = []
  write = direct.durable_write
  def observed_write(path, data, mode):
    if path == direct.STATUS:
      states.append(json.loads(data)['state'])
    write(path, data, mode)
  monkeypatch.setattr(direct, 'durable_write', observed_write)
  direct.serve(trial=40)
  assert attempts == [0., 30.]  # Failure backoff, not a busy reconnect loop.
  assert 'active' in states and 'waiting_offroad' not in states
  assert len([c for c in s.calls if c[:4] == ('--wait', '12', 'connection', 'up')]) == 1
  s.wireless.close.assert_called_once_with()


def test_slow_ap_launcher_is_detached_and_does_not_wait_in_usb_owner(tmp_path, monkeypatch):
  monkeypatch.setattr(direct, 'STATUS', tmp_path / 'absent')
  child = NS(wait=Mock(side_effect=AssertionError('must not wait for AP')),
             communicate=Mock(side_effect=AssertionError('must not wait for AP')))
  launch = Mock(return_value=child)
  monkeypatch.setattr(direct.subprocess, 'Popen', launch)
  direct.ensure()
  assert launch.call_args.kwargs['start_new_session'] is True
  child.wait.assert_not_called()
  child.communicate.assert_not_called()


def test_network_worker_drops_inherited_realtime_and_stays_off_model_core(monkeypatch):
  calls = []
  monkeypatch.setattr(os, 'setpriority', lambda *a: calls.append(('nice', *a)))
  monkeypatch.setattr(os, 'sched_setscheduler', lambda *a: calls.append(('scheduler', *a)))
  monkeypatch.setattr(os, 'sched_setaffinity', lambda *a: calls.append(('affinity', *a)))
  monkeypatch.setattr(os, 'cpu_count', lambda: 8)
  direct.low_priority()
  assert ('nice', os.PRIO_PROCESS, 0, 19) in calls
  scheduler = next(c for c in calls if c[0] == 'scheduler')
  assert scheduler[2] == os.SCHED_OTHER and scheduler[3].sched_priority == 0
  assert ('affinity', 0, {0, 1, 2, 3}) in calls


def test_boot_provisioning_keeps_usb_control_flowing_while_ap_activates(monkeypatch):
  clock = [0.]
  class Done(Exception):
    pass
  class Channel:
    def setblocking(self, value):
      assert value is False
    def send(self, data):
      stamp, = HEADER.unpack_from(data)
      packets.append((stamp, json.loads(data[HEADER.size:])))
  packets, queries = [], []
  priority = Mock()
  def config(owner, onroad):
    priority.assert_called_once_with()
    queries.append(clock[0])
    return {'version': 1, 'owner': owner, 'onroad': onroad, 'wifi_policy': 'direct_only',
            'direct_subnet': '172.29.77.0/24' if clock[0] >= 5 else None,
            'profiles': [{'synthetic': True}] if clock[0] >= 5 else []}
  def sleep(seconds):
    clock[0] += seconds
    if clock[0] >= 8:
      raise Done()
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: NS(get=lambda *_: True)))
  monkeypatch.setattr(publisher.socket, 'socket', lambda **k: Channel())
  monkeypatch.setattr(publisher.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(publisher.time, 'sleep', sleep)
  monkeypatch.setattr(publisher, 'provisioning', config)
  monkeypatch.setattr(publisher, 'selected_release', lambda *_: {'synthetic': True})
  monkeypatch.setattr(direct, 'ensure', Mock())
  monkeypatch.setattr(direct, 'low_priority', priority)
  with pytest.raises(Done):
    publisher.main(0)
  assert queries == [0., 6.]  # AP readiness is not hidden behind 30s refresh.
  assert [t for t, _ in packets] == [0., 2., 4., 6.]
  assert all(p['onroad'] is True and p['jetson_release'] == {'synthetic': True} for _, p in packets)
  assert packets[0][1]['profiles'] == [] and packets[-1][1]['profiles'] == [{'synthetic': True}]
