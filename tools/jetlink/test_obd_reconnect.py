"""Synthetic BLE recovery timing; no radio, pairing, or vehicle access."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b


def test_no_prior_response_keeps_five_second_unlimited_attempts():
  retry = b.ReconnectDelay()
  retry.select('cfg')
  assert [retry.failed(True) for _ in range(100)] == [5.] * 100


def test_flapping_uses_only_two_fast_attempts_without_reset_on_each_connect():
  retry = b.ReconnectDelay()
  retry.select('cfg')
  delays = []
  for now in range(100, 110):
    retry.received(float(now))
    delays.append(retry.failed(True))
  assert delays == [1., 2.] + [5.] * 8


@pytest.mark.parametrize('healthy_seconds,expected', [(29.9, 5.), (30., 1.)])
def test_thirty_seconds_of_reception_rearms_fast_recovery(healthy_seconds, expected):
  retry = b.ReconnectDelay()
  retry.select('cfg')
  retry.received(100.)
  assert retry.failed(True) == 1.
  assert retry.failed(True) == 2.
  for i in range(30):
    retry.received(110. + i)
  retry.received(110. + healthy_seconds)
  assert retry.failed(True) == expected


def test_uncertain_cleanup_and_configuration_changes_cannot_use_fast_recovery():
  retry = b.ReconnectDelay()
  retry.select('cfg')
  retry.received(100.)
  assert retry.failed(False) == retry.failed(None) == retry.failed(True) == 5.
  for i in range(31):
    retry.received(110. + i)
  assert retry.failed(True) == 1.
  retry.select('other-cfg')
  assert retry.failed(True) == 5.
  retry.received(float('nan'))
  assert retry.failed(True) == 5.


def test_long_silence_cannot_be_counted_as_thirty_seconds_of_healthy_data():
  retry = b.ReconnectDelay()
  retry.select('cfg')
  retry.received(100.)
  assert retry.failed(True) == 1.
  assert retry.failed(True) == 2.
  retry.received(110.)
  retry.received(140.)
  assert retry.failed(True) == 5.


@pytest.mark.parametrize('connected,disconnect_fails,expected', [
  (('b', False), False, True), (('b', False), True, True),
  (('b', True), False, False), (('b', 0), False, False),
  (('s', 'false'), False, False), (None, False, False),
])
def test_close_confirms_target_disconnected_even_after_radio_loss(connected, disconnect_fails, expected):
  calls = []
  link = b.Link(NS(), '00:11:22:33:44:55')
  link.device = '/org/bluez/hci0/dev_00_11_22_33_44_55'
  link.uart, link.notifying = link.device + '/uart', True
  async def call(path, interface, method, signature=None, body=(), **kwargs):
    calls.append((path, method))
    assert kwargs['timeout'] == .5
    if method == 'Disconnect' and disconnect_fails:
      raise ConnectionError('already disconnected')
    if method == 'Get':
      assert body == (b.DEVICE, 'Connected')
      return (connected,)
    return ()
  link.call = call
  assert asyncio.run(link.close()) is expected
  assert calls[-2:] == [(link.device, 'Disconnect'), (link.device, 'Get')]
  assert all(path in (link.device, link.uart) for path, _ in calls)


@pytest.mark.parametrize('fault', ['missing', 'timeout', 'filter'])
def test_failed_release_confirmation_never_qualifies_for_fast_retry(fault):
  link = b.Link(NS(), '00:11:22:33:44:55')
  link.device = '/org/bluez/hci0/dev_00_11_22_33_44_55'
  async def call(path, interface, method, *args, **kwargs):
    if method == 'Get':
      raise TimeoutError if fault == 'timeout' else ValueError
    return ()
  link.call = call
  if fault == 'filter':
    link.filter = NS(__exit__=lambda *args: (_ for _ in ()).throw(ValueError('filter cleanup failed')))
    with pytest.raises(ValueError):
      asyncio.run(link.close())
  else:
    assert asyncio.run(link.close()) is False


@pytest.mark.parametrize('cleanup_verified,expected', [(True, [1., 2., 5., 5., 5.]), (False, [5.] * 5)])
def test_real_run_waits_for_old_router_and_target_before_each_fast_attempt(monkeypatch, cleanup_verified, expected):
  import jeepney.io.asyncio
  clock, sessions, events, delays, reports = [100.], [], [], [], []
  @asynccontextmanager
  async def router(*args):
    try:
      yield None
    finally:
      events.append('router-exit')
  class Session:
    def __init__(self, router, address):
      assert address == '00:11:22:33:44:55'
      assert all(session.closed for session in sessions)
      self.closed, self.polls = False, 0
      sessions.append(self)
    async def connect(self):
      if len(sessions) > 1:
        raise ConnectionError('radio still unavailable')
    async def close(self):
      self.closed = True
      events.append('close')
      return cleanup_verified
  class Poller:
    last_result = 'ok'
    def delay(self, link):
      return .05
    async def poll(self, link, **kwargs):
      link.polls += 1
      if link.polls > 1:
        raise b.QueryError('elm_prompt_timeout')
      kwargs['publish'](clock[0], {'soc_percent': 77.}, {'soc_percent': clock[0]})
  async def sleep(delay):
    clock[0] += delay
    if delay <= b.POWER_PERIOD:
      return
    assert events[-2:] == ['close', 'router-exit']
    assert all(session.closed for session in sessions)
    delays.append(delay)
    if len(delays) == 5:
      raise asyncio.CancelledError
  cfg = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'Link', Session)
  monkeypatch.setattr(b, 'MetricPoller', Poller)
  monkeypatch.setattr(b, 'read_json', lambda _: cfg)
  monkeypatch.setattr(b, 'vehicle_monitor', lambda: None)
  monkeypatch.setattr(b, 'write_snapshot', lambda state, *a, **kw: reports.append((state, kw)))
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  monkeypatch.setattr(b.asyncio, 'sleep', sleep)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert delays == expected and len(sessions) == 5
  failures = [report['diagnostics'] for state, report in reports if state == 'unavailable']
  assert [report['retry_delay_s'] for report in failures] == expected
  assert all(report['cleanup_verified'] is cleanup_verified for report in failures)
