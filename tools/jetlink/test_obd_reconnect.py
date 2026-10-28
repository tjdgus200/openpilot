"""Synthetic BLE recovery timing; no radio, pairing, or vehicle access."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b


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
def test_failed_release_confirmation_still_reports_failure(fault):
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


@pytest.mark.parametrize('cleanup_verified,connect_started,proof', [
  (True, True, True), (False, True, False), (None, True, False),
  (False, False, True), (TimeoutError, False, False), (TimeoutError, True, False),
  (ValueError, True, False),
])
def test_real_run_retries_at_100ms_after_each_old_local_attempt_exits(monkeypatch, cleanup_verified, connect_started, proof):
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
      if cleanup_verified in (TimeoutError, ValueError):
        raise cleanup_verified('synthetic cleanup failure')
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
    if len(delays) == 100:
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
  assert delays == [.1] * 100 and len(sessions) == 100
  failures = [report['diagnostics'] for state, report in reports if state == 'unavailable']
  assert [report['retry_delay_s'] for report in failures] == [.1] * 100
  assert all(report['cleanup_verified'] is proof for report in failures)


@pytest.mark.parametrize('fault', ['configuration', 'router'])
def test_failures_before_link_creation_also_retry_at_100ms(monkeypatch, fault):
  import jeepney.io.asyncio
  delays, reports = [], []
  @asynccontextmanager
  async def router(*args):
    raise ConnectionError('synthetic router unavailable')
    yield  # pragma: no cover
  def read_config(_):
    if fault == 'configuration':
      raise FileNotFoundError('synthetic configuration missing')
    return {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  async def sleep(delay):
    delays.append(delay)
    assert delay == .1
    if len(delays) == 5:
      raise asyncio.CancelledError
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'read_json', read_config)
  monkeypatch.setattr(b, 'write_snapshot', lambda state, **kw: reports.append((state, kw)))
  monkeypatch.setattr(b.asyncio, 'sleep', sleep)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert delays == [.1] * 5
  failures = [kw['diagnostics'] for state, kw in reports if state == 'unavailable']
  assert len(failures) == 5
  assert all(report['cleanup_verified'] is False for report in failures)
