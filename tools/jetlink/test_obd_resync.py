"""Discard rotated transactions, then verify a local ELM boundary; no radio IO."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b
from test_obd_battery import notification, response
from test_obd_response_count import sample


def rotated(command, offset=1):
  frames = response(sample(command, 127))[:-1].split()
  return b'\r' + b'\r'.join(frames[-offset:] + frames[:-offset]) + b'\r>'


@pytest.mark.parametrize('command', tuple(b.REPLIES))
def test_all_rotations_identified_but_never_accepted_as_payload(command):
  for offset in range(1, len(response(sample(command))[:-1].split())):
    raw = rotated(command, offset)
    assert b.rotated_reply(raw, command)
    with pytest.raises(ValueError):
      b.parse_reply(raw, command)
  assert not b.rotated_reply(response(sample(command)), command)


@pytest.mark.parametrize('mutate', [
  lambda raw: raw[:-1], lambda raw: raw + b'>', lambda raw: raw + b'X',
  lambda raw: raw.replace(b'7EC', b'7ED', 1),
  lambda raw: raw.replace(b'103E', b'103F', 1),
  lambda raw: raw.replace(b'620101', b'620105', 1),
  lambda raw: raw.replace(b'7EC21', b'7EC23', 1),
  lambda raw: raw.replace(b'\r7EC22', b'\rSTOPPED\r7EC22', 1),
  lambda raw: raw[:-22] + b'>', lambda raw: raw[:-1] + raw,
  lambda raw: b'x' * 4097 + raw, lambda raw: b'\xff' + raw,
])
def test_other_malformed_or_mixed_replies_do_not_enter_recovery(mutate):
  assert not b.rotated_reply(mutate(rotated('220101')), '220101')
  assert not b.rotated_reply(rotated('220101'), '220105')


class ClockLink(b.Link):
  def __init__(self, script, monkeypatch):
    super().__init__(None, '00:11:22:33:44:55')
    self.script, self.writes, self.clock = list(script), [], [100.]
    monkeypatch.setattr(b, 'time', NS(monotonic=lambda: self.clock[0]))
    async def sleep(delay):
      self.clock[0] += delay
    monkeypatch.setattr(b.asyncio, 'sleep', sleep)

  async def call(self, path, interface, method, signature=None, body=(), **kwargs):
    assert method == 'WriteValue' and body[1] == {'type': ('s', 'request')}
    self.writes.append(body[0])
    raw = self.script.pop(0)
    if isinstance(raw, Exception):
      raise raw
    self.clock[0] += .1
    for start in range(0, len(raw), 20):
      self.queue.put_nowait(notification(raw[start:start+20]))
    return ()


def test_bad_power_never_replaces_last_good_value_or_refreshes_its_age(monkeypatch):
  async def check():
    link = ClockLink([rotated('220101'), b'ELM327 v2.3\r>', response(sample('220105'))], monkeypatch)
    poller = b.MetricPoller()
    poller.values = {'pack_power_kw': 8., 'soc_percent': 70.}
    poller.observed = {'pack_power_kw': 99., 'soc_percent': 99.}
    publications = []
    await poller.poll(link, publish=lambda *args: publications.append(args))
    assert link.writes == [b'2201019\r', b'ATI\r']
    assert poller.last_result == 'resynchronized'
    assert poller.resync_count == poller.resync_streak == 1
    assert publications[-1][1] == {'pack_power_kw': 8., 'soc_percent': 70.}
    assert publications[-1][2] == {'pack_power_kw': 99., 'soc_percent': 99.}
    # No immediate duplicate ECU query; the normal scheduler owns the next slot.
    assert b.REQUEST_GAP_MIN <= poller.delay(link) <= b.POWER_PERIOD
    link.clock[0] = link.next_request
    await poller.poll(link)
    assert link.writes[-1] == b'2201057\r'
    assert poller.resync_streak == 0 and poller.resync_count == 1
    assert poller.values['soc_percent'] == 0. and poller.values['pack_power_kw'] == 8.
    assert poller.observed['pack_power_kw'] == 99.
    assert b.collection_info(link, poller)['resync_count'] == 1
  asyncio.run(check())


def test_two_consecutive_recoveries_only_then_existing_fatal_path(monkeypatch):
  async def check():
    script = [rotated('220105'), b'ELM327 v2.3\r>'] * 2 + [rotated('220105')]
    link, poller = ClockLink(script, monkeypatch), b.MetricPoller()
    for _ in range(2):
      link.clock[0] = max(link.clock[0], poller.retry_at.get('220105', 0.))
      assert await poller.read(link, '220105') == (True, None)
    link.clock[0] = max(link.clock[0], poller.retry_at['220105'])
    with pytest.raises(b.QueryError, match='elm_resync_limit'):
      await poller.read(link, '220105')
    assert link.writes == [b'2201057\r', b'ATI\r', b'2201057\r', b'ATI\r', b'2201057\r']
    assert poller.resync_count == 2 and not poller.values
  asyncio.run(check())


@pytest.mark.parametrize('identity', [b'OK\r>', b'?\r>', response(sample('220105')),
                                      b'ELM327 v2.3\r>extra', TimeoutError(), OSError()])
def test_failed_identity_or_transport_never_releases_another_poll(monkeypatch, identity):
  async def check():
    link, poller = ClockLink([rotated('220105'), identity], monkeypatch), b.MetricPoller()
    with pytest.raises(b.QueryError):
      await poller.read(link, '220105')
    assert link.writes == [b'2201057\r', b'ATI\r'] and not poller.values
  asyncio.run(check())


def test_quarantined_late_tail_is_discarded_before_identity_and_never_becomes_data(monkeypatch):
  async def check():
    link = ClockLink([rotated('220105'), b'ELM327 v2.3\r>', response(sample('220105'))], monkeypatch)
    poller = b.MetricPoller()
    async def tail():
      await original_sleep(.01)
      link.queue.put_nowait(notification(b'7EC2600000000000000\r'))
    task = asyncio.create_task(tail())
    assert await poller.read(link, '220105') == (True, None)
    await task
    assert link.writes == [b'2201057\r', b'ATI\r'] and not poller.values
    assert link.queue.empty() and not link.queue.overflow
    link.clock[0] = poller.retry_at['220105']
    assert await poller.read(link, '220105') == (True, sample('220105'))
    assert poller.resync_streak == 0
  original_sleep = asyncio.sleep
  asyncio.run(check())


@pytest.mark.parametrize('mode', ['overflow', 'bytes', 'chunks'])
def test_quarantine_limits_are_fatal_and_send_no_identity(monkeypatch, mode):
  async def check():
    link = ClockLink([], monkeypatch)
    if mode == 'overflow':
      link.queue.overflow = True
    else:
      for _ in range(9 if mode == 'bytes' else 32):
        link.queue.put_nowait(notification(b'x' * (512 if mode == 'bytes' else 1)))
    with pytest.raises(b.QueryError):
      await link.resynchronize()
    assert not link.writes
  asyncio.run(check())


def test_continuous_stream_cannot_extend_quarantine_beyond_deadline(monkeypatch):
  async def check():
    link = ClockLink([], monkeypatch)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async def chatter():
      while True:
        link.queue.put_nowait(notification(b'x'))
        await original_sleep(.15)
    producer = asyncio.create_task(chatter())
    try:
      with pytest.raises(b.QueryError, match='elm_resync_busy'):
        await asyncio.wait_for(link.resynchronize(), 2.5)
      assert loop.time() - started < 2.5 and not link.writes
    finally:
      producer.cancel()
      with pytest.raises(asyncio.CancelledError):
        await producer
  original_sleep = asyncio.sleep
  asyncio.run(check())


def test_unrecognized_bad_reply_remains_fatal_without_sync_command(monkeypatch):
  async def check():
    invalid = response(sample('220105')).replace(b'7EC21', b'7EC23', 1)
    link, poller = ClockLink([invalid], monkeypatch), b.MetricPoller()
    with pytest.raises(ValueError):
      await poller.read(link, '220105')
    assert link.writes == [b'2201057\r']
  asyncio.run(check())
