"""Synthetic BLE recovery timing; no radio, pairing, or vehicle access."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b


def test_released_attempts_have_no_retry_cap_or_rearm_timer():
  # Cold start and repeated flapping use the same delay without a prior reply.
  assert [b.reconnect_delay(True) for _ in range(1000)] == [.1] * 1000


@pytest.mark.parametrize('proof', [False, None, 0, 1, 'true'])
def test_unknown_release_keeps_five_second_delay(proof):
  assert b.reconnect_delay(proof) == 5.


def test_new_release_proof_resumes_fast_retry_immediately():
  assert [b.reconnect_delay(proof) for proof in (True, False, None, True, True)] == [.1, 5., 5., .1, .1]


def test_weak_signal_does_not_disconnect_a_responsive_target():
  from test_obd_battery import DiscoveryLink
  class WeakLink(DiscoveryLink):
    rssi = -42
    async def objects(self):
      objects = await super().objects()
      objects[self.target][b.DEVICE]['RSSI'] = ('n', self.rssi)
      return objects
  async def check():
    link = WeakLink()
    await link.connect()
    link.rssi = -120
    # Exercise both the new property observation and ongoing ELM transactions.
    for _ in range(3):
      await link.objects()
      assert await link.command('ATI') == b'ELM327 v2.3\r>'
    assert not any(method == 'Disconnect' for _, method in link.calls)
    await link.close()
    assert sum(method == 'Disconnect' for _, method in link.calls) == 1
  asyncio.run(check())


class NotConnected(Exception):
  name = 'org.bluez.Error.NotConnected'


@pytest.mark.parametrize('connected,disconnect_error,expected', [
  (('b', False), None, True), (('b', False), NotConnected, True),
  (('b', False), ConnectionError, False), (('b', False), TimeoutError, False),
  (('b', True), None, False), (('b', True), NotConnected, False),
  (('b', 0), None, False), (('s', 'false'), None, False), (None, None, False),
])
def test_close_requires_disconnect_completion_and_explicit_false(connected, disconnect_error, expected):
  calls = []
  link = b.Link(NS(), '00:11:22:33:44:55')
  link.device = '/org/bluez/hci0/dev_00_11_22_33_44_55'
  link.uart, link.notifying = link.device + '/uart', True
  async def call(path, interface, method, signature=None, body=(), **kwargs):
    calls.append((path, method))
    assert kwargs['timeout'] == .5
    if method == 'Disconnect' and disconnect_error:
      raise disconnect_error('synthetic disconnect result')
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


@pytest.mark.parametrize('cleanup_verified,connect_started,expected', [
  (True, True, .1), (False, True, 5.), (None, True, 5.),
  (False, False, .1), (TimeoutError, False, 5.), (TimeoutError, True, 5.),
])
def test_real_run_waits_for_old_router_and_target_before_each_attempt(monkeypatch, cleanup_verified, connect_started, expected):
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
      self.connect_started = connect_started
      sessions.append(self)
    async def connect(self):
      if not connect_started or len(sessions) > 1:
        raise ConnectionError('radio still unavailable')
    async def close(self):
      self.closed = True
      events.append('close')
      if cleanup_verified is TimeoutError:
        raise TimeoutError('synthetic cleanup timeout')
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
    if delay == .05:
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
  assert delays == [expected] * 5 and len(sessions) == 5
  failures = [report['diagnostics'] for state, report in reports if state == 'unavailable']
  assert [report['retry_delay_s'] for report in failures] == [expected] * 5
  assert all(report['cleanup_verified'] is (expected == .1) for report in failures)
