import math

import pytest

from opendbc.car.hyundai.steering_recovery import SimpleSteeringRecovery, TIMING_SETTINGS, read_recovery_settings


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
  assert trace.step() == pytest.approx(26.5)
  previous = trace.previous
  for _ in range(180):
    value = trace.step()
    assert 0 <= value - previous <= 1.5 + 1e-9
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
  assert trace.step(driver_ratio=.7) == pytest.approx(previous + .75)
  assert trace.step(active=False, legacy=0) == 0
  assert trace.step() == 25
  assert not trace.helper.released


def test_faster_full_range_recovery_keeps_confirmation_and_has_no_overshoot():
  times = []
  for seconds in (1.5, 1.2):
    trace = Trace()
    trace.step(pressed=True, recovery_ms=int(seconds * 1000))
    start = trace.now
    samples = []
    for _ in range(200):
      samples.append(trace.step(recovery_ms=int(seconds * 1000)))
      if trace.previous == 250.:
        break
    assert samples[:19] == [25.] * 19
    assert all(25. <= value <= 250. for value in samples)
    assert samples == sorted(samples)
    times.append(trace.now - start)
  assert times[0] == pytest.approx(1.7, abs=.02)
  assert times[1] == pytest.approx(1.4, abs=.02)
  assert times[0] - times[1] == pytest.approx(.3, abs=.01)


@pytest.mark.parametrize('setting', TIMING_SETTINGS)
def test_timing_settings_accept_every_documented_step_and_default_corrupt_values(setting):
  from types import SimpleNamespace

  name, minimum, maximum, step, default = setting
  index = 0 if name == 'Pv5SteerReleaseMs' else 1
  for value in range(minimum, maximum + 1, step):
    params = SimpleNamespace(get=lambda key, value=value: value if key == name else 0)
    assert read_recovery_settings(params)[index] == value
  for value in (None, False, True, '', '100', 0, -1, minimum - 1, maximum + 1, minimum + 1, math.nan, math.inf):
    params = SimpleNamespace(get=lambda key, value=value: value)
    assert read_recovery_settings(params)[index] == default
  for exception in (ValueError, TypeError, OverflowError, OSError):
    def unreadable(key, exception=exception):
      raise exception('unreadable test setting')
    assert read_recovery_settings(SimpleNamespace(get=unreadable))[index] == default


@pytest.mark.parametrize('release_ms', range(100, 501, 50))
@pytest.mark.parametrize('recovery_ms', (1000, 1500, 3000))
def test_configured_confirmation_and_ramp_bounds(release_ms, recovery_ms):
  trace = Trace()
  args = {'release_ms': release_ms, 'recovery_ms': recovery_ms}
  trace.step(**args)
  for _ in range(release_ms // 10 - 1):
    assert trace.step(**args) == 25
  rate = 225 / (recovery_ms / 1000)
  assert trace.step(**args) == pytest.approx(25 + rate * .01)
  previous = trace.previous
  for _ in range(recovery_ms // 10):
    value = trace.step(**args)
    assert 0 <= value - previous <= rate * .01 + 1e-9
    assert 25 <= value <= 250
    previous = value
  assert value == 250


def test_live_edit_keeps_current_confirmation_and_ramp_until_next_release():
  trace = Trace()
  trace.step()
  slow = {'release_ms': 500, 'recovery_ms': 3000}
  fast = {'release_ms': 100, 'recovery_ms': 1000}
  for _ in range(20):
    assert trace.step(**slow) == 25
  for _ in range(29):
    assert trace.step(**fast) == 25
  assert trace.step(**fast) == pytest.approx(25.75)
  previous = trace.previous
  assert trace.step(**fast) == pytest.approx(previous + .75)
  # Immediate yielding re-arms the next low-effort episode with the new values.
  assert trace.step(pressed=True, **fast) == 25
  for _ in range(9):
    assert trace.step(**fast) == 25
  assert trace.step(**fast) == pytest.approx(27.25)
  previous = trace.previous
  assert trace.step(enabled=False, **fast) == previous
  previous = trace.previous
  assert trace.step(enabled=False, **fast) == pytest.approx(previous + 1.5)


@pytest.mark.parametrize('reset', ({'valid': False}, {'driver_ratio': .7}, {'pre_override_yield': .5}))
def test_interrupted_confirmation_restarts_full_new_duration(reset):
  trace = Trace()
  trace.step()
  for _ in range(15):
    trace.step(release_ms=200)
  trace.step(**reset)
  for _ in range(49):
    assert trace.step(release_ms=500) == 25
  assert trace.step(release_ms=500) == pytest.approx(26.5)


def test_direct_invalid_timings_also_default_without_unbounded_rise():
  trace = Trace()
  for _ in range(21):
    trace.step(release_ms=0, recovery_ms=math.nan)
  assert trace.previous == pytest.approx(26.5)
  assert trace.helper.release_seconds == .2
  assert trace.helper.recovery_seconds == 1.5


def test_web_native_defaults_and_trial_ranges_match_controller():
  import json
  from pathlib import Path

  root = Path(__file__).resolve().parents[5]
  catalog = json.loads((root / 'openpilot/selfdrive/carrot_settings.json').read_text(encoding='utf8'))
  registry = (root / 'openpilot/common/params_keys.h').read_text(encoding='utf8')
  for name, minimum, maximum, step, default in TIMING_SETTINGS:
    item = next(item for item in catalog['params'] if item['name'] == name)
    assert (item['min'], item['max'], item['unit'], item['default']) == (minimum, maximum, step, default)
    assert item['detail_parent'] == 'Pv5SimpleSteerRecovery'
    assert item['display_unit'] == 'timeMs'
    assert f'{{"{name}", {{PERSISTENT, INT, "{default}"}}}}' in registry
