"""ELM response-count replay, with a synthetic adapter; no radio/vehicle access."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b
from test_obd_battery import notification, payload, response


COUNTS = {'220101': 9, '220105': 7, '220106': 6,
          '220102': 6, '220103': 6, '220104': 6, '22010A': 6}


def sample(command, marker=0):
  length, prefix = b.REPLIES[command]
  return prefix + bytes([marker]) * (length - len(prefix))


@pytest.mark.parametrize('command,count', COUNTS.items())
def test_count_covers_entire_fixed_contract(command, count):
  raw = response(sample(command))
  assert len(raw[:-1].split()) == count
  assert b.request_bytes(command) == (command + str(count) + '\r').encode('ascii')
  assert b.parse_reply(raw, command) == sample(command)


def test_identity_and_setup_are_unchanged_and_not_general_diagnostics():
  for command in ('ATI', *b.SETUP):
    assert b.request_bytes(command) == (command + '\r').encode('ascii')
  for command in ('2201019', '2201011', '220114', 'ATZ', '2E010100', '220101\rATI'):
    with pytest.raises(ValueError, match='not allowed'):
      b.request_bytes(command)


@pytest.mark.parametrize('length', [True, 6, 105, 4096])
def test_unrepresentable_contract_never_sends_an_underestimated_count(monkeypatch, length):
  monkeypatch.setitem(b.REPLIES, '220101', (length, b'\x62\x01\x01'))
  with pytest.raises(ValueError, match='unsupported response count'):
    b.request_bytes('220101')


class CountAdapter(b.Link):
  """Emulates the documented ELM count limit, not proof of physical behavior.

  Bare requests receive a complete reply followed by seven frames of a second
  reply before the prompt, matching the owner's observed structure. Counted
  requests return the requested number of CAN lines and one terminal prompt.
  """
  def __init__(self, fragment, *, ignores_count=False, reply=None):
    super().__init__(None, '00:11:22:33:44:55')
    self.fragment, self.ignores_count, self.reply = fragment, ignores_count, reply
    self.writes = []

  async def call(self, path, interface, method, signature=None, body=(), **kwargs):
    assert method == 'WriteValue' and body[1] == {'type': ('s', 'request')}
    wire = body[0].decode('ascii').rstrip('\r')
    self.writes.append(wire)
    command = wire[:6]
    first = response(sample(command, len(self.writes)))[:-1]
    second = b'\r'.join(response(sample(command, 127))[:-1].split()[:7]) + b'\r'
    lines = (first + second).split()
    if len(wire) == 7 and not self.ignores_count:
      lines = lines[:int(wire[-1], 16)]
    raw = self.reply if self.reply is not None else b'\r' + b'\r'.join(lines) + b'\r>'
    for start in range(0, len(raw), self.fragment):
      self.queue.put_nowait(notification(raw[start:start + self.fragment]))
    return ()


def test_old_bare_request_reproduces_the_reported_sequence_rejection(monkeypatch):
  monkeypatch.setattr(b, 'request_bytes', lambda command: (command + '\r').encode('ascii'))
  async def check():
    link = CountAdapter(20)
    raw = await link.command('220101')
    assert len(raw) == 322 and len(raw[:-1].split()) == 16
    with pytest.raises(ValueError, match='ISO-TP sequence'):
      b.parse_reply(raw)
  asyncio.run(check())


@pytest.mark.parametrize('fragment', [20, 31, 64, 512])
def test_counted_session_preserves_every_payload_and_did_across_fragmentation(fragment):
  async def check():
    link = CountAdapter(fragment)
    # Avoid real scheduler waits; late-data protection is tested separately.
    for command in (*COUNTS, '220101', '220105'):
      link.next_request = 0
      raw = await link.command(command)
      assert b.parse_reply(raw, command) == sample(command, len(link.writes))
      assert link.queue.empty() and not link.queue.overflow
    assert link.writes == [c + str(COUNTS[c]) for c in (*COUNTS, '220101', '220105')]
  asyncio.run(check())


def test_adapter_ignoring_count_still_rejects_mixed_reply():
  async def check():
    link = CountAdapter(20, ignores_count=True)
    with pytest.raises(ValueError, match='ISO-TP sequence'):
      b.parse_reply(await link.command('220101'))
  asyncio.run(check())


@pytest.mark.parametrize('raw', [b'NO DATA\r>', b'?\r>', response(payload())[:-21] + b'>',
                                 response(payload()).replace(b'620101', b'620105', 1),
                                 response(payload()).replace(b'7EC21', b'7EC22', 1)])
def test_count_suffix_never_turns_missing_or_invalid_data_into_metrics(raw):
  async def check():
    reply = await CountAdapter(64, reply=raw).command('220101')
    if b.no_data(reply):
      assert reply == b'NO DATA\r>'
    with pytest.raises(ValueError):
      b.parse_reply(reply)
  asyncio.run(check())


def test_late_second_response_blocks_next_request_without_reusing_its_bytes(monkeypatch):
  clock = [100.]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  async def check():
    link = CountAdapter(20)
    assert b.parse_reply(await link.command('220101')) == sample('220101', 1)
    async def sleep(delay):
      clock[0] += delay
      link.queue.put_nowait(notification(response(sample('220101', 99))))
    monkeypatch.setattr(b.asyncio, 'sleep', sleep)
    with pytest.raises(b.QueryError, match='unsolicited'):
      await link.command('220105')
    assert link.writes == ['2201019']
    # The replacement connection owns its own queue and returns a new payload.
    fresh = CountAdapter(20)
    assert b.parse_reply(await fresh.command('220105'), '220105') == sample('220105', 1)
  asyncio.run(check())
