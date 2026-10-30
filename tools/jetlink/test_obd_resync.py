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


TAIL_DIDS = ('220102', '220103', '220104', '220106', '22010A')


def prefixed_tail(command, marker=127):
  # Frozen protocol positions from the owner's captures, independent of the
  # production count/length table. All payload bytes here are synthetic.
  length = 38 if command == '220106' else 39
  first = bytes((0x10, length, 0x62, 0x01, int(command[4:], 16)))
  frames = [bytes((0x26,)), first, bytes((0x21,)), bytes((0x22,)), bytes((0x23,)), bytes((0x24,))]
  return b'\r' + b'\r'.join(b'7EC' + f.ljust(8, bytes((marker,))).hex().upper().encode() for f in frames) + b'\r>'


@pytest.mark.parametrize('command', TAIL_DIDS)
def test_observed_missing_final_frame_is_discard_only(command):
  raw = prefixed_tail(command)
  assert len(raw) == 122
  assert not b.rotated_reply(raw, command)
  with pytest.raises(ValueError):
    b.parse_reply(raw, command)
  assert b.prefixed_tail_reply(raw, command)
  assert not b.prefixed_tail_reply(response(sample(command)), command)


@pytest.mark.parametrize('command', TAIL_DIDS)
def test_observed_tail_recovers_same_link_without_reusing_failed_payload(monkeypatch, command):
  async def check():
    link = ClockLink([prefixed_tail(command), b'ELM327 v2.3\r>', response(sample(command, 126))], monkeypatch)
    poller = b.MetricPoller()
    poller.values, poller.observed = {'pack_power_kw': 8.}, {'pack_power_kw': 99.}
    # Unpublished multi-page assemblies must not cross a lost boundary.
    poller.first_temperature_page = (99., sample('220101'))
    poller.cell_blocks, poller.cell_started = {'220102': sample('220102')}, 99.
    assert await poller.read(link, command) == (True, None)
    assert link.writes == [b.request_bytes(command), b'ATI\r']
    assert poller.values == {'pack_power_kw': 8.} and poller.observed == {'pack_power_kw': 99.}
    assert poller.first_temperature_page is None and not poller.cell_blocks
    assert poller.resync_count == poller.resync_streak == 1
    assert poller.next_cells >= link.clock[0] + 1.
    link.clock[0] = poller.retry_at[command]
    assert await poller.read(link, command) == (True, sample(command, 126))
    assert poller.last_result == 'ok' and poller.resync_streak == 0
    assert link.writes == [b.request_bytes(command), b'ATI\r', b.request_bytes(command)]
  asyncio.run(check())


@pytest.mark.parametrize('mutate', [
  lambda raw: raw[:-1], lambda raw: raw + b'>', lambda raw: raw + b'X',
  lambda raw: raw.replace(b'7EC', b'7ED', 1),
  lambda raw: raw.replace(b'1027620102', b'1026620102', 1),
  lambda raw: raw.replace(b'620102', b'620103', 1),
  lambda raw: raw.replace(b'7EC26', b'7EC27', 1),
  lambda raw: raw.replace(b'7EC21', b'7EC22', 1),
  lambda raw: raw.replace(b'7EC22', b'7EC10', 1),
  lambda raw: raw.replace(b'7EC24', b'7EC25', 1),
  lambda raw: raw.replace(b'\r7EC22', b'\rSTOPPED\r7EC22', 1),
  lambda raw: raw[:-1] + b'7EC2500000000000000\r>',
  lambda raw: raw[:-22] + b'>', lambda raw: raw[:-1] + raw,
  lambda raw: b'x' * 4097 + raw, lambda raw: b'\xff' + raw,
])
def test_incomplete_tail_recognition_does_not_relax_other_guards(mutate):
  assert not b.prefixed_tail_reply(mutate(prefixed_tail('220102')), '220102')


@pytest.mark.parametrize('command', ['ATI', '220101', '220105', '220114'])
def test_incomplete_tail_does_not_broaden_other_contracts(command):
  assert not b.prefixed_tail_reply(prefixed_tail('220102'), command)


@pytest.mark.parametrize('identity', [b'OK\r>', b'?\r>', prefixed_tail('220102'),
                                      b'ELM327 v2.3\r>extra', TimeoutError(), OSError()])
def test_incomplete_tail_failed_identity_retains_fatal_path(monkeypatch, identity):
  async def check():
    link, poller = ClockLink([prefixed_tail('220102'), identity], monkeypatch), b.MetricPoller()
    with pytest.raises(b.QueryError):
      await poller.read(link, '220102')
    assert link.writes == [b'2201026\r', b'ATI\r'] and not poller.values
    assert poller.resync_count == 0
  asyncio.run(check())


def test_mixed_boundary_faults_share_two_recovery_limit(monkeypatch):
  async def check():
    link = ClockLink([prefixed_tail('220102'), b'ELM327 v2.3\r>', rotated('220105'),
                      b'ELM327 v2.3\r>', prefixed_tail('220106')], monkeypatch)
    poller = b.MetricPoller()
    for command in ('220102', '220105'):
      assert await poller.read(link, command) == (True, None)
    with pytest.raises(b.QueryError, match='elm_resync_limit'):
      await poller.read(link, '220106')
    assert link.writes == [b'2201026\r', b'ATI\r', b'2201057\r', b'ATI\r', b'2201066\r']
    assert poller.resync_count == poller.resync_streak == 2 and not poller.values
  asyncio.run(check())


def test_cancellation_during_incomplete_tail_recovery_sends_no_further_command(monkeypatch):
  async def check():
    link, poller = ClockLink([prefixed_tail('220102')], monkeypatch), b.MetricPoller()
    task = asyncio.create_task(poller.read(link, '220102'))
    await original_sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert link.writes == [b'2201026\r'] and poller.resync_count == 0
    assert not poller.values and not poller.observed
  original_sleep = asyncio.sleep
  asyncio.run(check())


def test_repeated_good_and_retired_transactions_preserve_exact_payloads(monkeypatch):
  async def check():
    commands = TAIL_DIDS * 10
    script, expected = [], []
    for i, command in enumerate(commands):
      failed = i % 10 == 0
      script.extend([prefixed_tail(command), b'ELM327 v2.3\r>'] if failed else [response(sample(command, i))])
      expected.append(None if failed else sample(command, i))
    link, poller = ClockLink(script, monkeypatch), b.MetricPoller()
    for command, payload in zip(commands, expected, strict=True):
      link.clock[0] = max(link.clock[0], poller.retry_at.get(command, 0.))
      assert await poller.read(link, command) == (True, payload)
    assert not link.script and link.queue.empty() and not link.queue.overflow
    assert poller.resync_count == 5 and poller.resync_streak == 0
    assert len(link.writes) == 55 and link.writes.count(b'ATI\r') == 5
    assert not poller.values and not poller.observed
  asyncio.run(check())


def test_cell_poll_keeps_retired_generation_backoff_and_previous_complete_array(monkeypatch):
  async def check():
    link = ClockLink([prefixed_tail('220103'), b'ELM327 v2.3\r>'], monkeypatch)
    poller = b.MetricPoller(details=True)
    poller.due = dict.fromkeys(poller.due, 103.)
    poller.cell_blocks, poller.cell_started = {'220102': sample('220102')}, 99.
    cells = [3.96] * 108
    poller.values, poller.observed = {'cell_voltages_v': cells}, {'cell_voltages_v': 99.}
    publications = []
    await poller.poll(link, parked=True, publish=lambda *args: publications.append(args))
    assert link.writes == [b'2201036\r', b'ATI\r']
    assert not poller.cell_blocks and poller.next_cells >= 102.
    assert publications[-1][1] == {'cell_voltages_v': cells}
    assert publications[-1][2] == {'cell_voltages_v': 99.}
    # Recovery cannot create or renew a heatmap. The prior generation expires
    # at the same collector age, even if no subsequent good response arrives.
    link.clock[0] = 104.1
    assert poller.snapshot()[1:] == ({}, {})
  asyncio.run(check())
