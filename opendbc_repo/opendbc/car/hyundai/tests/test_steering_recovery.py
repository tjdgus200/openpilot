import math

import pytest

from opendbc.car.hyundai.steering_recovery import SimpleSteeringRecovery


class Trace:
  def __init__(self, seed=25.0):
    self.helper = SimpleSteeringRecovery()
    self.now = 0.0
    self.previous = seed

  def step(self, **kw):
    self.now += .01
    args = dict(enabled=True, now=self.now, legacy=250.0, previous=self.previous,
                minimum=25.0, maximum=250.0, active=True, valid=True, pressed=False,
                driver_ratio=0.0, pre_override_yield=0.0)
    self.previous = self.helper.update(**(args | kw))
    return self.previous


def test_default_off_never_changes_legacy_or_samples_time():
  trace = Trace()
  for cap in (0.0, 25.0, 72.1, 250.0):
    assert trace.step(enabled=False, legacy=cap, valid=False, now=float('nan')) == cap
  assert trace.helper.last_time is None


@pytest.mark.parametrize('seed', (25.0, 80.0, 180.0, 250.0))
def test_enable_is_seeded_without_authority_step(seed):
  trace = Trace(seed)
  assert trace.step() == seed
  assert trace.step() <= seed + 1.5 + 1e-9


def test_fixed_recovery_requires_low_force_then_has_bounded_rise():
  trace = Trace()
  assert trace.step() == 25
  for _ in range(19):
    assert trace.step() == 25
  assert trace.step() == pytest.approx(26.875)
  previous = trace.previous
  for _ in range(180):
    value = trace.step()
    assert 0 <= value - previous <= 1.875 + 1e-9
    previous = value
  assert previous == 250


@pytest.mark.parametrize('pressed,pre_yield,fall', ((True, 0, 20), (False, .5, 5), (False, 1, 10)))
def test_driver_yield_is_immediate_even_during_return(pressed, pre_yield, fall):
  trace = Trace(180)
  trace.step()
  assert trace.step(pressed=pressed, pre_override_yield=pre_yield) == 180 - fall
  assert trace.step(enabled=False, legacy=250, pressed=pressed, pre_override_yield=pre_yield) <= 180 - fall
  last = trace.previous
  assert trace.step(enabled=False, pressed=pressed, pre_override_yield=pre_yield) <= last - fall


def test_turning_off_cannot_jump_to_background_legacy_cap():
  trace = Trace(50)
  trace.step()
  assert trace.step(enabled=False) == 50
  assert trace.step(enabled=False) == pytest.approx(51.5)
  for _ in range(180):
    trace.step(enabled=False)
  assert not trace.helper.returning
  assert trace.step(enabled=False, legacy=137.0) == 137.0


@pytest.mark.parametrize('issue', ('stale', 'gap', 'time_reversed', 'nan', 'infinity'))
def test_faults_cannot_restore_authority(issue):
  trace = Trace(80)
  trace.step()
  args = {'valid': False} if issue == 'stale' else {}
  if issue == 'gap':
    trace.now += 1
  elif issue == 'time_reversed':
    trace.now -= 1
  elif issue in ('nan', 'infinity'):
    args['driver_ratio'] = float('nan') if issue == 'nan' else math.inf
  assert trace.step(**args) <= 25
  assert not trace.helper.released


def test_driver_force_taper_stops_recovery_and_reactivation_clears_history():
  trace = Trace(80)
  trace.step()
  for _ in range(21):
    trace.step()
  previous = trace.previous
  assert trace.step(driver_ratio=.8) == previous
  assert trace.step(driver_ratio=.7) == pytest.approx(previous + .9375)
  assert trace.step(active=False, legacy=0) == 0
  assert trace.step() == 25
  assert not trace.helper.released


def test_faster_full_range_recovery_keeps_confirmation_and_has_no_overshoot(monkeypatch):
  from opendbc.car.hyundai import steering_recovery

  times = []
  for seconds in (1.5, 1.2):
    monkeypatch.setattr(steering_recovery, 'RECOVERY_SECONDS', seconds)
    trace = Trace()
    trace.step(pressed=True)
    start = trace.now
    samples = []
    for _ in range(200):
      samples.append(trace.step())
      if trace.previous == 250.:
        break
    assert samples[:19] == [25.] * 19
    assert all(25. <= value <= 250. for value in samples)
    assert samples == sorted(samples)
    times.append(trace.now - start)
  assert times[0] == pytest.approx(1.7, abs=.02)
  assert times[1] == pytest.approx(1.4, abs=.02)
  assert times[0] - times[1] == pytest.approx(.3, abs=.01)
