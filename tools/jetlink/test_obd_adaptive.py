"""Single-response publication and paced/fault-injected ELM transactions."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/jetlink')]
from openpilot.selfdrive.carrot import obd_battery as b
from test_obd_battery import payload, response, soc_payload, coolant_payload, notification

DATA = {'220101': payload(), '220105': soc_payload(), '220106': coolant_payload()}


def test_publish_each_reply_before_next_request_or_later_failure(monkeypatch):
  clock, events, published = [100.], [], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      events.append(('request', command))
      if command == '220106':
        raise TimeoutError
      clock[0] += .1
      return response(DATA[command])
  def publish(stamp, values, times):
    published.append((stamp, values, times))
    events.append(('published', len(published)))
  async def check():
    with pytest.raises(TimeoutError):
      await b.MetricPoller().poll(Link(), publish=publish)
  asyncio.run(check())
  assert events == [('request', '220101'), ('published', 1), ('request', '220105'),
                    ('published', 2), ('request', '220106')]
  assert 'pack_power_kw' in published[0][1] and 'soc_percent' not in published[0][1]
  assert 'soc_percent' in published[1][1] and 'coolant_inlet_c' not in published[1][1]
  assert published[0][2]['pack_power_kw'] == published[1][2]['pack_power_kw'] == 100.
  assert published[1][0] == published[1][2]['soc_percent']


def test_complete_no_data_only_backs_off_that_did_and_never_fabricates_values(monkeypatch):
  clock, commands, published = [100.], [], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      commands.append((clock[0], command))
      if command == '220106' and clock[0] < 106.:
        return b'NO DATA\r>'
      return response(DATA[command])
  async def check():
    poller = b.MetricPoller()
    for frame in range(13):
      clock[0] = 100. + frame * .5
      await poller.poll(Link(), publish=lambda *sample: published.append(sample))
      if clock[0] < 106.:
        assert 'coolant_inlet_c' not in poller.values
      assert len(poller.retry_at) <= len(b.REPLIES)
    assert poller.values['coolant_inlet_c'] == 27
    assert not poller.failures and not poller.retry_at and poller.no_data_count == 2
  asyncio.run(check())
  assert [t for t, c in commands if c == '220106'] == [100., 102., 106.]
  assert len([c for _, c in commands if c == '220101']) == 13
  assert len(published) == len(commands)


@pytest.mark.parametrize('bad', [b'NO DATA\r>>', b'NO DATA\r>junk', b'NO DATA\r7EC0000\r>',
                               b'NO DATA\r', b'STOPPED\r>', b'CAN ERROR\r>', b'NO DATA' + b' '*4096 + b'>'])
def test_only_complete_exact_no_data_is_recoverable(bad):
  assert not b.no_data(bad)
  class Link:
    async def command(self, command):
      return bad
  async def check():
    with pytest.raises(ValueError):
      await b.MetricPoller().poll(Link())
  asyncio.run(check())


def test_no_data_cell_scan_is_discarded_then_restarted_without_mixing(monkeypatch):
  clock, commands, fail = [100.], [], [True]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      commands.append(command)
      if command == '220103' and fail[0]:
        fail[0] = False
        return b'NO DATA\r>'
      raw = DATA[command] if command in DATA else bytes.fromhex('62'+command[2:])+bytes(4)+bytes([194]*32)
      return response(raw)
  async def check():
    poller = b.MetricPoller(details=True)
    for now in (100., 100.5):
      clock[0] = now
      await poller.poll(Link(), parked=True)
    assert not poller.cell_blocks and 'cell_voltages_v' not in poller.values
    for now in (101., 101.5, 102.):
      clock[0] = now
      before = len(commands)
      await poller.poll(Link(), parked=True)
      assert not any(c in dict(b.CELL_BLOCKS) for c in commands[before:])
    for now in (102.5, 103., 103.5, 104.):
      clock[0] = now
      await poller.poll(Link(), parked=True)
    assert len(poller.values['cell_voltages_v']) == 108
    assert poller.observed['cell_voltages_v'] == 102.5
  asyncio.run(check())


def test_real_run_keeps_responsive_no_data_session_open_until_cancelled(monkeypatch):
  import jeepney.io.asyncio
  states, connections, closed = [], [], []
  @asynccontextmanager
  async def router(*_):
    yield None
  class Link:
    def __init__(self, *args):
      connections.append(self)
    async def connect(self):
      pass
    async def command(self, command):
      return b'NO DATA\r>'
    async def close(self):
      closed.append(self)
  async def stop(delay):
    assert b.REQUEST_GAP_MIN <= delay <= b.POWER_PERIOD
    raise asyncio.CancelledError
  cfg = {'version':1, 'address':'00:11:22:33:44:55', 'confirmed_fields':[]}
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'Link', Link)
  monkeypatch.setattr(b, 'read_json', lambda _: cfg)
  monkeypatch.setattr(b, 'vehicle_monitor', lambda: NS(update=lambda _:None))
  monkeypatch.setattr(b, 'write_snapshot', lambda state, *a, **k: states.append(state))
  monkeypatch.setattr(b.asyncio, 'sleep', stop)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert len(connections) == 1 and closed == connections
  assert 'unavailable' not in states and 'receiving' not in states
  assert states.count('waiting_data') == 3


def test_pacing_adapts_to_latency_and_no_data_without_overlap(monkeypatch):
  clock, writes, sleeps, replies = [100.], [], [], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  async def sleep(delay):
    sleeps.append(delay)
    clock[0] += delay
  monkeypatch.setattr(b.asyncio, 'sleep', sleep)
  class Link(b.Link):
    async def call(self, path, interface, method, signature=None, body=(), **kwargs):
      assert method == 'WriteValue' and body[1] == {'type':('s','request')}
      writes.append(clock[0])
      delay, raw = replies.pop(0)
      clock[0] += delay
      self.queue.put_nowait(notification(raw))
      return ()
  async def check():
    link = Link(None, '00:11:22:33:44:55')
    replies.append((.02, response()))
    await link.command('220101')
    assert link.request_gap == b.REQUEST_GAP_MIN
    finish, gap = clock[0], link.request_gap
    replies.append((.02, b'NO DATA\r>'))
    await link.command('220106')
    assert writes[-1] - finish == pytest.approx(gap)
    assert link.request_gap == .1
    replies.append((.8, response()))
    await link.command('220101')
    assert link.request_gap == pytest.approx(.2)
    for _ in range(8):
      replies.append((.02, b'NO DATA\r>'))
      await link.command('220106')
      assert link.request_gap <= b.REQUEST_GAP_MAX
    assert link.request_gap == b.REQUEST_GAP_MAX
    replies.append((.02, response()))
    await link.command('220101')
    assert b.REQUEST_GAP_MIN <= link.request_gap < b.REQUEST_GAP_MAX
    assert len(sleeps) == len(writes) - 1
  asyncio.run(check())


def test_late_data_during_quiet_gap_blocks_next_write(monkeypatch):
  clock, writes = [100.], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link(b.Link):
    async def call(self, *args, **kwargs):
      writes.append(1)
      self.queue.put_nowait(notification(response()))
      return ()
  async def check():
    link = Link(None, '00:11:22:33:44:55')
    await link.command('220101')
    async def sleep(delay):
      clock[0] += delay
      link.queue.put_nowait(notification(b'late>'))
    monkeypatch.setattr(b.asyncio, 'sleep', sleep)
    with pytest.raises(b.QueryError, match='unsolicited'):
      await link.command('220105')
    assert writes == [1]
  asyncio.run(check())


@pytest.mark.parametrize('failure,code', [('write_timeout', 'gatt_write_timeout'),
                                         ('write_error', 'gatt_write_failed'), ('prompt', 'elm_prompt_timeout')])
def test_transport_failures_remain_fatal_and_have_bounded_categories(monkeypatch, failure, code):
  clock = [100.]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link(b.Link):
    async def call(self, *args, **kwargs):
      if failure == 'write_timeout':
        raise TimeoutError
      if failure == 'write_error':
        raise OSError('must never expose private exception details')
      clock[0] += 4.  # Expire the reply deadline without waiting in real time.
      return ()
  async def check():
    with pytest.raises(b.QueryError) as exc:
      await Link(None, '00:11:22:33:44:55').command('220101')
    assert exc.value.code == code and str(exc.value) == code
  asyncio.run(check())


def test_metadata_and_ram_snapshot_stay_bounded_without_raw_data(tmp_path):
  poller = b.MetricPoller()
  poller.last_command = poller.last_no_data_command = '220106'
  poller.last_result, poller.no_data_count = 'no_data', 65535
  metadata = b.collection_info(None, poller)
  values = {'cell_voltages_v': [3.88]*108, 'module_temps_c': [20]*15,
            'pack_voltage_v':420., 'pack_current_a':-125., 'pack_power_kw':-52.5,
            'soc_percent':70., 'max_temp_c':25., 'min_temp_c':20., 'charge_mode':'off',
            'coolant_inlet_c':22., 'cell_delta_mv':20.}
  path = tmp_path / 'snapshot'
  b.write_snapshot('receiving', values, frozenset(values), path, 'fixture', observed_at=100.,
                   field_times=dict.fromkeys(values,100.), diagnostics=metadata)
  record = b.read_json(path)
  assert record['collection']['no_data_request'] == '220106'
  assert path.stat().st_size < 4096 and 'address' not in record['collection']
  assert b.snapshot_packet(record,100.,'fixture')['fields']['pack_power_kw']['value'] == -52.5
