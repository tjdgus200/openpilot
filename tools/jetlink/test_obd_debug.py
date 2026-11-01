"""Fault-injected raw diagnostic observations; no live Bluetooth/CAN requests."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b, obd_debug as d


IDENTITY = 'a' * 64


def lease():
  return {'version': 1, 'config_id': IDENTITY, 'nonce': '0123456789abcdef', 'start': 90., 'until': 690.}


def monitor(now=100.):
  class Monitor(dict):
    def update(self, _):
      pass
  sm = Monitor(carState=NS(canValid=True, canTimeout=False, gearShifter='park', vEgo=0., vEgoRaw=0.),
               carControl=NS(enabled=False, latActive=False, longActive=False), selfdriveState=NS(active=False, enabled=False))
  sm.valid = dict.fromkeys(sm, True)
  sm.alive = dict.fromkeys(sm, True)
  sm.recv_time = dict.fromkeys(sm, now)
  return sm


def reply(key, marker=0):
  _, rx, cmd, length = d.CONTRACTS[key]
  payload = bytes.fromhex('62' + cmd[2:]) + bytes((i + marker) % 256 for i in range(length - 3))
  frames = [bytes((0x10, length)) + payload[:6]]
  frames += [bytes((0x20 | index,)) + payload[start:start + 7].ljust(7, b'\0')
             for index, start in enumerate(range(6, length, 7), 1)]
  return b'\r'.join(rx.encode() + frame.hex().upper().encode() for frame in frames) + b'\r>'


@pytest.mark.parametrize('target', [t[0] for t in d.TARGETS])
def test_exact_source_length_did_count_and_offsets(target):
  value = d.parse(reply(target), target)
  assert value == bytes(range(d.CONTRACTS[target][3] - 3))
  tx, rx, cmd, length = d.CONTRACTS[target]
  link = d.DiagnosticLink(None, '00:11:22:33:44:55')
  link.debug_rx = rx
  assert link.encode_request(cmd) == (cmd + format((length + 7) // 7, 'X') + '\r').encode()
  assert link.encode_request('ATSH' + tx) == ('ATSH' + tx + '\r').encode()
  assert link.encode_request('ATCRA' + rx) == ('ATCRA' + rx + '\r').encode()


@pytest.mark.parametrize('target', [t[0] for t in d.TARGETS])
@pytest.mark.parametrize('fault', ['source', 'did', 'sequence', 'length', 'extra', 'missing', 'prefix', 'prompt', 'unicode'])
def test_malformed_payloads_never_display(target, fault):
  raw = reply(target)
  rx = d.CONTRACTS[target][1].encode()
  raw = {
    'source': lambda: raw.replace(rx, b'7EF', 1),
    'did': lambda: raw.replace(b'62', b'63', 1),
    'sequence': lambda: raw.replace(rx + b'21', rx + b'22', 1),
    'length': lambda: raw.replace(rx + b'10', rx + b'11', 1),
    'extra': lambda: raw[:-1] + raw.split(b'\r')[1] + b'\r>',
    'missing': lambda: b'\r'.join(raw.split(b'\r')[:-2]) + b'\r>',
    'prefix': lambda: raw.split(b'\r')[1] + b'\r' + raw,
    'prompt': lambda: raw + b'junk',
    'unicode': lambda: b'\xff' + raw,
  }[fault]()
  with pytest.raises(ValueError):
    d.parse(raw, target)


@pytest.mark.parametrize('command', ['ATZ', 'ATMA', '2E010100', '310101', 'ATSH000', '220199', '220114\rATI'])
def test_no_scan_reset_write_or_arbitrary_ecu(command):
  with pytest.raises(ValueError):
    d.DiagnosticLink(None, '00:11:22:33:44:55').encode_request(command)


def test_normal_reader_retains_original_allowlist():
  for command in b.COMMANDS:
    assert b.Link(None, 'target').encode_request(command) == b.request_bytes(command)
  for command in ('220100', '220114', 'ATSH7B3', 'ATCRA7BB'):
    with pytest.raises(ValueError):
      b.Link(None, 'target').encode_request(command)


@pytest.mark.parametrize('fault', ['moving', 'raw_moving', 'nan', 'drive', 'can_invalid', 'can_timeout', 'enabled', 'lat',
                                   'long', 'active', 'sd_enabled', 'stale', 'future', 'not_alive', 'not_valid'])
def test_motion_control_or_stale_state_revokes(fault):
  sm = monitor()
  if fault in ('moving', 'raw_moving', 'nan', 'drive', 'can_invalid', 'can_timeout'):
    field, value = {'moving': ('vEgo', .1), 'raw_moving': ('vEgoRaw', .1), 'nan': ('vEgo', float('nan')),
                    'drive': ('gearShifter', 'drive'), 'can_invalid': ('canValid', False), 'can_timeout': ('canTimeout', True)}[fault]
    setattr(sm['carState'], field, value)
  elif fault in ('enabled', 'lat', 'long'):
    setattr(sm['carControl'], {'enabled': 'enabled', 'lat': 'latActive', 'long': 'longActive'}[fault], True)
  elif fault in ('active', 'sd_enabled'):
    setattr(sm['selfdriveState'], 'active' if fault == 'active' else 'enabled', True)
  elif fault == 'stale':
    sm.recv_time['carState'] = 99.74
  elif fault == 'future':
    sm.recv_time['carControl'] = 101.
  elif fault == 'not_alive':
    sm.alive['selfdriveState'] = False
  else:
    sm.valid['carControl'] = False
  assert not d.parked(sm, 100.)


def test_lease_bounds_nonce_identity_and_clock():
  value = lease()
  assert d.valid_lease(value, IDENTITY, 100.)
  for altered in (dict(value, until=691.), dict(value, until=99.), dict(value, start=101.), dict(value, version=True),
                  dict(value, nonce='raw-data'), dict(value, start=float('nan')), dict(value, extra=1)):
    assert not d.valid_lease(altered, IDENTITY, 100.)
  assert not d.valid_lease(value, 'other', 100.)
  assert not d.valid_lease(value, IDENTITY, 89.)
  assert not d.valid_lease(value, IDENTITY, 690.)


def test_default_off_never_initializes_monitor(monkeypatch, tmp_path):
  control = d.Control(tmp_path / 'missing')
  assert control.active(IDENTITY) is None
  assert control.monitor is None


def test_round_robin_filter_switching_rate_and_bounded_ram(monkeypatch, tmp_path):
  clock, calls = [100.], []
  monkeypatch.setattr(d, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      calls.append(command)
      if command.startswith('AT'):
        clock[0] += .1
        return b'OK\r>'
      key = next(t[0] for t in d.TARGETS if (t[2], t[3]) == (self.debug_rx, command))
      return reply(key)
  async def check():
    poller, link = d.Poller(lease(), IDENTITY, lambda: True, tmp_path / 'status'), Link()
    for _ in range(120):
      clock[0] += .5
      await poller.poll(link)
      before = len(calls)
      await poller.poll(link)
      assert len(calls) == before  # No catch-up or immediate second transaction.
    assert len(poller.rows) == 6
    assert len((tmp_path / 'status').read_bytes()) < 4096
    assert calls[:8] == ['220101', '220106', '220114', 'ATSH7B3', 'ATCRA7BB', '220100', '220101', '220102']
    assert calls[8:10] == ['ATSH7E4', 'ATCRA7EC']
  asyncio.run(check())


@pytest.mark.parametrize('index', [0, 3])
def test_revoked_during_filter_or_reply_cannot_issue_next_query_or_publish(monkeypatch, tmp_path, index):
  clock = [100.]
  monkeypatch.setattr(d, 'time', NS(monotonic=lambda: clock[0]))
  allowed, calls = [True], []
  class Link:
    async def command(self, command):
      calls.append(command)
      allowed[0] = False
      return b'OK\r>' if command.startswith('AT') else reply('7EC/0101')
  poller = d.Poller(lease(), IDENTITY, lambda: allowed[0], tmp_path / 'status')
  poller.index = index
  with pytest.raises(ValueError, match='configuration changed'):
    asyncio.run(poller.poll(Link()))
  assert len(calls) == 1
  assert not poller.path.exists()


def test_no_data_does_not_invent_values(monkeypatch, tmp_path):
  monkeypatch.setattr(d, 'time', NS(monotonic=lambda: 100.))
  class Link:
    async def command(self, _):
      return b'NO DATA\r>'
  poller = d.Poller(lease(), IDENTITY, lambda: True, tmp_path / 'status')
  asyncio.run(poller.poll(Link()))
  assert poller.rows == {'7EC/0101': {'state': 'no_data', 'at': 100.}}


def test_malformed_response_is_discarded_and_resync_cap(monkeypatch, tmp_path):
  clock, resync = [100.], []
  monkeypatch.setattr(d, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      return b'broken\r>'
    async def resynchronize(self):
      resync.append(True)
  async def check():
    poller = d.Poller(lease(), IDENTITY, lambda: True, tmp_path / 'status')
    for _ in range(2):
      await poller.poll(Link())
      clock[0] += 1
    assert len(resync) == 2
    assert all(row['state'] == 'invalid' and 'data' not in row for row in poller.rows.values())
    with pytest.raises(b.QueryError):
      await poller.poll(Link())
  asyncio.run(check())


def display(tmp_path):
  config = tmp_path / 'config'
  config.write_text(json.dumps({'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}))
  view = d.Display(tmp_path / 'request', tmp_path / 'status', config)
  value = dict(lease(), config_id=b.config_id(b.configuration(b.read_json(config))))
  d.atomic_write(view.request, value)
  return view, value


def status(view, value, *, at=100., marker=0):
  d.atomic_write(view.status, {'version': 1, 'nonce': value['nonce'], 'config_id': value['config_id'], 'updated': at,
                               'rows': {'7EC/0101': {'state': 'ok', 'at': at, 'data': d.parse(reply('7EC/0101', marker), '7EC/0101').hex().upper()}}})


def test_ui_byte_changes_request_removal_and_freshness(tmp_path):
  view, value = display(tmp_path)
  status(view, value)
  assert view.update(100., True)['7EC/0101']['state'] == 'ok'
  assert not view.changed
  status(view, value, at=101., marker=1)
  assert view.update(101., True)
  assert len(view.changed) == 59
  assert view.update(106.1, True) == {}  # Lost collector, no renewed timestamp.
  view.request.unlink()
  assert view.update(107.2, True) is None
  assert not view.previous and not view.changed


@pytest.mark.parametrize('fault', ['nonce', 'identity', 'extra_page', 'oversize', 'future', 'bad_hex', 'bad_length', 'bool_time'])
def test_ui_rejects_malformed_snapshot(tmp_path, fault):
  view, value = display(tmp_path)
  status(view, value)
  row = d.read(view.status)
  if fault == 'nonce':
    row['nonce'] = 'f' * 16
  elif fault == 'identity':
    row['config_id'] = 'wrong'
  elif fault == 'extra_page':
    row['rows']['7EF/0000'] = {}
  elif fault == 'oversize':
    row['padding'] = 'x' * 5000
  elif fault == 'future':
    row['updated'] = 101.
  elif fault == 'bad_hex':
    row['rows']['7EC/0101']['data'] = 'ZZ' * 59
  elif fault == 'bad_length':
    row['rows']['7EC/0101']['data'] = '00'
  elif fault == 'bool_time':
    row['rows']['7EC/0101']['at'] = True
  view.status.write_text(json.dumps(row))
  assert view.update(100., True) == {}
  assert view.update(100.1, False) is None


def test_mode_handoff_retires_connection_before_other_owner(monkeypatch, tmp_path):
  import jeepney.io.asyncio
  events, modes, count = [], [None, lease(), None], [0]
  @asynccontextmanager
  async def router(*args):
    yield None
    events.append('router_exit')
  class Control:
    def active(self, identity):
      return modes[min(count[0], 2)]
  class Link:
    connect_started = True
    def __init__(self, *args):
      events.append('create')
    async def connect(self): events.append('connect')
    async def close(self):
      events.append('close')
      return True
  class Poller:
    def __init__(self, *args): pass
    async def poll(self, *args, **kwargs):
      count[0] += 1
      if count[0] == 3:
        raise asyncio.CancelledError
    def delay(self, _): return 0
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(d, 'Control', Control)
  monkeypatch.setattr(d, 'DiagnosticLink', Link)
  monkeypatch.setattr(d, 'Poller', Poller)
  monkeypatch.setattr(d, 'STATUS', tmp_path / 'status')
  monkeypatch.setattr(b, 'Link', Link)
  monkeypatch.setattr(b, 'MetricPoller', Poller)
  monkeypatch.setattr(b, 'vehicle_monitor', lambda: None)
  monkeypatch.setattr(b, 'read_json', lambda _: {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []})
  monkeypatch.setattr(b, 'write_snapshot', lambda *a, **kw: None)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert events == ['create', 'connect', 'close', 'router_exit'] * 2 + ['create', 'connect', 'close']
