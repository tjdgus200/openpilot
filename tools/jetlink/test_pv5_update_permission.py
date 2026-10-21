"""Park/zero speed updates retain fresh CAN and boot-receiver boundaries."""

from contextlib import nullcontext
import sys
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.modeld.jetlink import startup, update_permission as policy


class Sample:
  def __init__(self):
    self.car = NS(canValid=True, canTimeout=False, vEgo=0.0, vEgoRaw=0.0, gearShifter='park')
    self.valid = {'carState': True}
    self.alive = {'carState': True}
    self.recv_time = {'carState': 100.0}

  def __getitem__(self, key):
    assert key == 'carState'
    return self.car

  def update(self, timeout):
    assert timeout == 0


@pytest.mark.parametrize(
  'field,value',
  [
    ('vEgoRaw', 0.001),
    ('vEgoRaw', -0.001),
    ('vEgoRaw', float('nan')),
    ('vEgoRaw', float('inf')),
    ('vEgoRaw', False),
    ('gearShifter', 'drive'),
    ('gearShifter', 'neutral'),
    ('canValid', False),
    ('canTimeout', True),
  ],
)
def test_motion_nonpark_or_bad_can_denies_permission(field, value):
  sample = Sample()
  setattr(sample.car, field, value)
  assert not policy.parked_stationary(sample, 100.1)


@pytest.mark.parametrize('failure', ['invalid', 'dead', 'old', 'future'])
def test_vehicle_information_must_be_fresh(failure):
  sample = Sample()
  if failure == 'invalid':
    sample.valid['carState'] = False
  if failure == 'dead':
    sample.alive['carState'] = False
  if failure == 'old':
    sample.recv_time['carState'] = 99.0
  if failure == 'future':
    sample.recv_time['carState'] = 101.0
  assert not policy.parked_stationary(sample, 100.1)


def test_only_park_and_zero_are_vehicle_conditions_not_ignition_or_cruise():
  sample = Sample()
  sample.car.cruiseState = NS(enabled=True)
  params = NS(get_bool=lambda key: key == 'IsOnroad')
  guard = policy.Permission(params, factory=lambda: sample, clock=lambda: 100.1)
  assert policy.SERVICES == ('carState',)
  assert guard.allowed()
  sample.car.vEgo = 0.03  # The stopped wheel speed is zero despite filter settling.
  assert guard.allowed()
  sample.car.gearShifter = 'drive'
  assert not guard.allowed()  # Rechecks inside the subscriber polling interval.
  assert not policy.Permission(params, factory=lambda: sample, clock=lambda: 100.1, allow_parked=False).allowed()


def test_offroad_remains_independent_and_conflicting_flags_fail_closed():
  assert policy.Permission(NS(get_bool=lambda key: key == 'IsOffroad')).allowed()
  assert not policy.Permission(NS(get_bool=lambda key: True)).allowed()
  assert not policy.Permission(NS(get_bool=lambda key: False)).allowed()


@pytest.mark.parametrize('move_after_transfer', [False, True])
def test_boot_gate_rechecks_park_before_activation(monkeypatch, move_after_transfer):
  from tools.jetlink import usb_release_client as sender  # noqa: TID251 -- standalone updater
  from tools.jetlink.usb_release import SHA256_CAPABILITY  # noqa: TID251 -- standalone updater

  sample, sent, calls, connected = Sample(), [], [], [True]
  params = NS(get_bool=lambda key: key == 'IsOnroad')
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: params))
  monkeypatch.setattr(startup, 'Permission', lambda p, **kw: policy.Permission(p, factory=lambda: sample, clock=lambda: 100.1, **kw))
  monkeypatch.setattr(startup, 'bulk_scheduling', nullcontext)
  manifest = {'delivery': 'usb', 'source_commit': 'a' * 40}
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **kw: manifest)

  def upload(client, value, cache, allowed, progress):
    calls.append(allowed())
    assert calls[-1]
    if move_after_transfer:
      sample.car.gearShifter = 'drive'

  monkeypatch.setattr(sender, 'upload', upload)
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, SHA256_CAPABILITY: True, 'engine_state': 'none', 'loaded': None}
  client = NS(t=NS(send_json=lambda *a: sent.append(a)), _next_seq=lambda: 1, state=lambda: {'carrot_update': {'state': 'ready'}})
  with pytest.raises(ConnectionError, match='USB setup' if move_after_transfer else 'reconnecting'):
    startup.wait_for_boot_update(client, peer, None, lambda: connected[0], lambda *a, **kw: None,
                                sleep=lambda _: connected.__setitem__(0, False))
  assert calls == [True]
  assert bool(sent) is not move_after_transfer


def test_running_engine_cannot_use_park_to_update(monkeypatch):
  sample, live = Sample(), [True]
  guard = policy.Permission(NS(get_bool=lambda key: key == 'IsOnroad'), factory=lambda: sample, clock=lambda: 100.1, allow_parked=False)
  assert not guard.allowed()
  with pytest.raises(ConnectionError):
    policy.wait_for_permission(guard, lambda: live[0], lambda: live.__setitem__(0, False), sleep=lambda _: None)


@pytest.mark.parametrize('field,value,reason', [('canTimeout', True, 'can_unavailable'),
                                             ('gearShifter', 'drive', 'gear_not_park'),
                                             ('vEgoRaw', .01, 'speed_not_zero')])
def test_waiting_reason_distinguishes_why_park_is_not_approved(field, value, reason):
  sample = Sample()
  setattr(sample.car, field, value)
  guard = policy.Permission(NS(get_bool=lambda key: key == 'IsOnroad'), factory=lambda: sample, clock=lambda: 100.1)
  assert not guard.allowed()
  assert guard.reason == reason
