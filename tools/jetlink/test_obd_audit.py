"""Additional lifecycle faults absent from the original nominal BLE fixtures."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b


@pytest.mark.parametrize('fault', ['notification', 'filter', 'discovery'])
def test_cleanup_failure_never_skips_target_disconnect_or_claims_success(fault):
  calls = []
  link = b.Link(None, '00:11:22:33:44:55')
  link.device, link.scan = '/target', '/adapter'
  link.uart, link.notifying = '/target/uart', True
  def exit_filter(*args):
    calls.append('filter')
    if fault == 'filter':
      raise ValueError('synthetic filter failure')
  link.filter = NS(__exit__=exit_filter)
  async def call(path, interface, method, *args, **kwargs):
    calls.append(method)
    assert kwargs['timeout'] == .5
    if (fault, method) in [('notification', 'StopNotify'), ('discovery', 'StopDiscovery')]:
      raise OSError('synthetic resource failure')
    return (('b', False),) if method == 'Get' else ()
  link.call = call
  assert asyncio.run(link.close()) is False
  assert calls == ['StopNotify', 'filter', 'StopDiscovery', 'Disconnect', 'Get']


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('unavailable_fails', [False, True])
def test_snapshot_io_failure_cannot_skip_cleanup_or_swallow_stop(monkeypatch, cancelled, unavailable_fails):
  import jeepney.io.asyncio
  events, reports, sleeps = [], [], []
  @asynccontextmanager
  async def router(*args):
    try:
      yield None
    finally:
      events.append('router_exit')
  class Session:
    connect_started = True
    def __init__(self, *args):
      pass
    async def connect(self):
      pass
    async def close(self):
      events.append('close')
      return True
  class Poller:
    async def poll(self, *args, **kwargs):
      if cancelled:
        raise asyncio.CancelledError
      raise ValueError('synthetic bad transaction')
  def snapshot(state, *args, **kwargs):
    reports.append(state)
    if state == 'disconnected' or state == 'unavailable' and unavailable_fails:
      raise OSError('synthetic no-space-left')
  async def sleep(delay):
    sleeps.append(delay)
    raise asyncio.CancelledError
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'Link', Session)
  monkeypatch.setattr(b, 'MetricPoller', Poller)
  monkeypatch.setattr(b, 'read_json', lambda _: config)
  monkeypatch.setattr(b, 'vehicle_monitor', lambda: None)
  monkeypatch.setattr(b, 'write_snapshot', snapshot)
  monkeypatch.setattr(b.asyncio, 'sleep', sleep)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert events == ['close', 'router_exit']
  assert sleeps == ([] if cancelled else [.1])
  assert ('unavailable' in reports) is not cancelled


def test_unexpected_snapshot_exception_still_attempts_close(monkeypatch):
  import jeepney.io.asyncio
  events = []
  @asynccontextmanager
  async def router(*args):
    try:
      yield None
    finally:
      events.append('router_exit')
  class Session:
    connect_started = True
    def __init__(self, *args):
      pass
    async def connect(self):
      raise ValueError('synthetic setup failure')
    async def close(self):
      events.append('close')
      return True
  def snapshot(state, *args, **kwargs):
    if state != 'connecting':
      raise MemoryError('synthetic allocation fault')
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'Link', Session)
  monkeypatch.setattr(b, 'read_json', lambda _: {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []})
  monkeypatch.setattr(b, 'write_snapshot', snapshot)
  with pytest.raises(MemoryError):
    asyncio.run(b.run())
  assert events == ['close', 'router_exit']


def test_cancelling_quarantine_sends_no_new_command(monkeypatch):
  calls = []
  link = b.Link(None, '00:11:22:33:44:55')
  async def call(*args, **kwargs):
    calls.append(True)
    return ()
  link.call = call
  async def check():
    task = asyncio.create_task(link.resynchronize())
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
  asyncio.run(check())
  assert not calls


@pytest.mark.parametrize('error,expected', [
  (ValueError('unexpected DID length'), 'reply_start_or_length'),
  (ValueError('ISO-TP sequence'), 'reply_sequence'), (ValueError('ELM setup'), 'elm_setup'),
  (ValueError('device identity'), 'device_identity'), (ValueError('snapshot size'), 'snapshot_size'),
  (TimeoutError('private'), 'deadline'), (OSError('private'), 'local_io'),
  (MemoryError('private'), 'memory'), (ModuleNotFoundError('private'), 'dependency'),
  (b.QueryError('private'), 'transport_or_recovery'),
  (ValueError('PRIVATE_SENTINEL' * 1000), 'other'), (RuntimeError('PRIVATE_SENTINEL'), 'other'),
])
def test_failure_detail_is_specific_but_never_discloses_source_material(error, expected):
  assert b.failure_detail(error) == expected


@pytest.mark.parametrize('headers,length', [
  (['26', '102E620105', '21', '22', '23', '24', '25'], 142),
  (['25', '26', '102E620105', '21', '22', '23', '24'], 142),
  (['103E620101', '21', '22', '23', '24', '25', '26', '27', '28',
    '103E620101', '21', '22', '23', '24', '25', '26'], 322),
])
def test_recorded_protocol_positions_independent_of_production_reply_table(headers, length):
  # Frozen positions/lengths from owner screenshots; all data bytes are
  # synthetic zeros. Do not derive this fixture from REPLIES or request_bytes.
  raw = b'\r' + b'\r'.join(b'7EC' + bytes.fromhex(header).ljust(8, b'\0').hex().upper().encode()
                          for header in headers) + b'\r>'
  assert len(raw) == length
  command = '220105' if length == 142 else '220101'
  with pytest.raises(ValueError):
    b.parse_reply(raw, command)
  assert b.rotated_reply(raw, command) is (length == 142)
