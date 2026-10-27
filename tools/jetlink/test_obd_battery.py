"""Synthetic protocol fixtures, not evidence of PV5 sensor calibration."""
import asyncio
import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b


def payload():
  data = bytearray(59)
  data[10:12] = (-125).to_bytes(2, 'big', signed=True)
  data[12:14] = (4226).to_bytes(2, 'big')
  data[14:16] = bytes([28, 25])
  data[23], data[25] = 200, 199
  return b'\x62\x01\x01' + data


def response(p=None):
  p = p or payload()
  frames = [bytes((0x10, len(p))) + p[:6]]
  for index, start in enumerate(range(6, len(p), 7), 1):
    frames.append(bytes([0x20 | index]) + p[start:start + 7].ljust(7, b'\0'))
  return b'\r'.join(b'7EC' + f.hex().upper().encode() for f in frames) + b'\r>'


def test_complete_transaction_and_offsets():
  assert b.parse_reply(response()) == payload()
  metrics = b.candidate_metrics(payload())
  assert metrics['max_temp_c'] == 28
  assert metrics['min_temp_c'] == 25
  assert metrics['pack_power_kw'] == pytest.approx(-5.2825)
  assert metrics['cell_delta_mv'] == 20
  assert 'soc_percent' not in metrics
  assert 'coolant_inlet_c' not in metrics


def coolant_payload(value=27):
  data = bytearray(35)
  data[4] = value & 255
  data[9] = 255  # Unknown mode, no guessed idle.
  return b'\x62\x01\x06' + data


def soc_payload(raw=150):
  data = bytearray(43)
  data[31] = raw
  return b'\x62\x01\x05' + data


@pytest.mark.parametrize('raw', [0, 1, 150, 200, 201, 255])
def test_soc_is_display_candidate_and_bounded(raw):
  p = b.parse_reply(response(soc_payload(raw)), '220105')
  assert b.soc_candidate(p) == ({'soc_percent': raw / 2} if raw <= 200 else {})
  with pytest.raises(ValueError):
    b.soc_candidate(payload())
  with pytest.raises(ValueError):
    b.parse_reply(response(soc_payload(raw)), '220106')


@pytest.mark.parametrize('raw,mode', [(0, 'idle'), (185, 'cooling'), (125, 'heating'), (100, None), (255, None)])
def test_thermal_mode_does_not_guess_unknown_or_electrical_power(raw, mode):
  p = bytearray(coolant_payload())
  p[12] = raw
  values = b.coolant_candidate(p)
  assert 'thermal_mode' not in values
  assert 'thermal_power_kw' not in values




def test_trend_confirmation_requires_maximum():
  with pytest.raises(ValueError, match='trend requires'):
    b.configuration({'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': ['temp_change_5min_c']})


@pytest.mark.parametrize('value,expected', [(27, 27), (-30, -30), (120, 120), (-31, None), (121, None), (-1, -1)])
def test_coolant_signed_layout_and_bounds(value, expected):
  p = b.parse_reply(response(coolant_payload(value)), '220106')
  assert b.coolant_candidate(p) == ({} if expected is None else {'coolant_inlet_c': expected})
  with pytest.raises(ValueError):
    b.parse_reply(response(coolant_payload(value)))
  with pytest.raises(ValueError):
    b.coolant_candidate(payload())


@pytest.mark.parametrize('raw', [
  response(coolant_payload())[:-1], response(coolant_payload()) + b'>',
  response(coolant_payload()).replace(b'7EC', b'7ED', 1),
  response(coolant_payload()).replace(b'7EC21', b'7EC22', 1),
  response(coolant_payload()).replace(b'1026', b'1027', 1),
  response(coolant_payload()).replace(b'620106', b'620101', 1),
  b'NO DATA\r>', b'7EC037F223100000000\r>', response(),
])
def test_coolant_bad_or_cross_did_reply_rejected(raw):
  with pytest.raises(ValueError):
    b.parse_reply(raw, '220106')


async def poll_round(poller, link, **kwargs):
  """At most four scheduler decisions; fixture clock is not advanced by sleeping."""
  for _ in range(4):
    result = await poller.poll(link, **kwargs)
  return result


def test_single_poll_is_one_transaction_and_keeps_individual_freshness(tmp_path, monkeypatch):
  clock = [100.0]
  monkeypatch.setattr(b.time, 'monotonic', lambda: clock[0])
  class PollLink:
    def __init__(self, failure=None):
      self.commands, self.failure = [], failure
    async def command(self, command):
      self.commands.append(command)
      if command == self.failure:
        raise TimeoutError('synthetic timeout')
      clock[0] += .1
      return response({'220101':payload(), '220105':soc_payload(), '220106':coolant_payload()}[command])
  async def check():
    link, poller = PollLink(), b.MetricPoller()
    for count in range(1,4):
      stamp, values, field_times = await poller.poll(link)
      assert len(link.commands) == count
    assert link.commands == ['220101','220105','220106']
    assert stamp == pytest.approx(100.2) and values['coolant_inlet_c'] == 27
    path = tmp_path / 'coolant.json'
    b.write_snapshot('receiving',values,frozenset(['coolant_inlet_c']),path,'cfg',observed_at=stamp,field_times=field_times)
    record = b.read_json(path)
    assert b.snapshot_packet(record,104.,'cfg')['fields']['coolant_inlet_c']['age_s'] == pytest.approx(3.8)
    assert not b.snapshot_packet(record,106.,'cfg')['fields']
    b.write_snapshot('receiving',values,path=path,identity='cfg',observed_at=stamp)
    fields = b.snapshot_packet(b.read_json(path),104.,'cfg')['fields']
    assert 'coolant_inlet_c' not in fields and 'charge_mode' in fields
    for failed, expected in [('220101',['220101']),('220106',['220101','220105','220106'])]:
      link = PollLink(failed)
      with pytest.raises(TimeoutError):
        await poll_round(b.MetricPoller(),link)
      assert link.commands == expected
  asyncio.run(check())


@pytest.mark.parametrize('mutate', [
  lambda x: x[:-1], lambda x: x + b'>', lambda x: x + b'X',
  lambda x: x.replace(b'7EC', b'7ED', 1),
  lambda x: x.replace(b'7EC21', b'7EC22', 1),
  lambda x: x.replace(b'7EC22', b'7EC21', 1),
  lambda x: x.replace(b'103E', b'1040', 1),
  lambda x: x.replace(b'620101', b'620105', 1),
  lambda x: b'NO DATA\r>', lambda x: x.replace(b'\r', b'\rSTOPPED\r', 1),
  lambda x: x.split(b'\r')[0] + b'\r>', lambda x: x[:-1] + x,
  lambda x: b'0' * 4097 + b'>', lambda x: b'\xff' + x,
])
def test_bad_transactions_rejected(mutate):
  with pytest.raises(ValueError):
    b.parse_reply(mutate(response()))


def test_implausible_fields_are_not_published():
  p = bytearray(payload())
  p[17], p[18] = 20, 30
  p[15:17] = b'\xff\xff'
  p[26], p[28] = 100, 200
  assert b.candidate_metrics(p) == {}


def test_config_no_arbitrary_targets_or_commands():
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  assert b.configuration(config) == ('00:11:22:33:44:55', frozenset())
  for change in ({'version': True}, {'address': '*'}, {'confirmed_fields': ['thermal_power_kw']},
                 {'confirmed_fields': ['max_temp_c', 'max_temp_c']}, {'command': 'ATZ'}):
    with pytest.raises(ValueError):
      b.configuration(config | change)


def test_snapshot_staleness_and_validation(tmp_path, monkeypatch):
  path = tmp_path / 'snapshot.json'
  monkeypatch.setattr(b.time, 'monotonic', lambda: 100.)
  b.write_snapshot('receiving', b.candidate_metrics(payload()), frozenset(['max_temp_c']), path, identity='test')
  record = b.read_json(path)
  packet = b.snapshot_packet(record, 102, 'test')
  assert packet['fields'] == {'max_temp_c': {'value': 28, 'age_s': 2.0}}
  assert not b.snapshot_packet(record, 106, 'test')['fields']
  assert not b.snapshot_packet(record, 99, 'test')['fields']
  assert not b.snapshot_packet(record, 102, 'another device/config')['fields']
  assert not b.snapshot_packet(record, 102)['fields']
  assert not b.snapshot_packet(record | {'state': 'unavailable'}, 101, 'test')['fields']
  assert not b.snapshot_packet(record | {'updated': float('nan')}, 101, 'test')['fields']
  assert not b.snapshot_packet(record | {'metrics': {}}, 101, 'test')['fields']
  path.write_text('x' * 4097)
  with pytest.raises(ValueError):
    b.read_json(path)


def notification(data):
  return NS(body=(b.GATT, {'Value': ('ay', data)}, []))


class FakeLink(b.Link):
  def __init__(self, chunks):
    super().__init__(None, '00:11:22:33:44:55')
    self.chunks = chunks
    self.writes = []

  async def call(self, path, interface, method, signature=None, body=(), **kwargs):
    self.writes.append(body[0])
    for chunk in self.chunks:
      try:
        self.queue.put_nowait(notification(chunk))
      except asyncio.QueueFull:
        pass
    return ()


def test_arbitrary_fragment_boundaries():
  async def check():
    raw = response()
    for size in (7, 20, 31, 64, 512):
      link = FakeLink([raw[i:i + size] for i in range(0, len(raw), size)])
      assert b.parse_reply(await link.command('220101')) == payload()
      assert link.writes == [b'220101\r']
  asyncio.run(check())


def test_overflow_late_data_and_prohibited_command():
  async def check():
    link = FakeLink([b'x'] * 33)
    with pytest.raises(ValueError, match='overflow'):
      await link.command('220101')
    link = FakeLink([response()])
    link.queue.put_nowait(notification(b'old>'))
    with pytest.raises(ValueError, match='unsolicited'):
      await link.command('220101')
    assert not link.writes
    fresh = FakeLink([response()])
    assert b.parse_reply(await fresh.command('220101')) == payload()
    with pytest.raises(ValueError, match='not allowed'):
      await fresh.command('2E010100')
  asyncio.run(check())


def test_timeout_and_failed_setup_never_follow_with_read(monkeypatch):
  async def check():
    link = FakeLink([])
    # Outer cancellation represents the same fail-closed path as the built-in
    # transaction timeout; no unbounded waiting and no next queued command.
    with pytest.raises(TimeoutError):
      await asyncio.wait_for(link.command('220101'), 0.01)
    assert len(link.writes) == 1
  asyncio.run(check())


def test_no_can_or_control_dependencies():
  source = Path(b.__file__).read_text()
  assert 'sendcan' not in source
  assert 'sub_sock' not in source
  assert b.COMMANDS - set(b.SETUP) == {'ATI', '220101', '220105', '220106', '220102', '220103', '220104', '22010A'}


def test_non_uart_notifications_do_not_block_and_oversize_fails():
  q = b.NotificationQueue()
  q.put_nowait(NS(body=(b.GATT, {'Notifying': ('b', True)}, [])))
  assert q.empty() and not q.overflow
  q.put_nowait(notification(b'x' * 513))
  assert q.empty() and q.overflow


class DiscoveryLink(b.Link):
  """Real connect orchestration/Jeepney rule API, fake D-Bus endpoints."""
  target = '/org/bluez/hci0/dev_00_11_22_33_44_55'

  def __init__(self, wrong_identity=False, setup_failure=False):
    from contextlib import nullcontext
    self.rules = []
    def filter_rule(rule, queue):
      self.rules.append(rule)
      return nullcontext(queue)
    super().__init__(NS(filter=filter_rule), '00:11:22:33:44:55')
    self.calls, self.commands = [], []
    self.wrong_identity, self.setup_failure = wrong_identity, setup_failure

  async def objects(self):
    def variants(**fields):
      return {k: ('s', v) for k, v in fields.items()}
    return {
      '/org/bluez/hci0': {b.ADAPTER: variants(Powered=True)},
      self.target: {b.DEVICE: variants(Address=self.address, Connected=True, ServicesResolved=True)},
      '/org/bluez/hci0/dev_other': {b.DEVICE: variants(Address='00:AA:BB:CC:DD:EE')},
      self.target + '/s': {'org.bluez.GattService1': variants(UUID=b.SERVICE)},
      self.target + '/s/uart': {b.GATT: variants(UUID=b.UART, Service=self.target + '/s', Flags=['write', 'notify'])},
      self.target + '/model': {b.GATT: variants(UUID='00002a24-0000-1000-8000-00805f9b34fb')},
      self.target + '/maker': {b.GATT: variants(UUID='00002a29-0000-1000-8000-00805f9b34fb')},
    }

  async def call(self, path, interface, method, signature=None, body=(), **kwargs):
    self.calls.append((path, method))
    if method == 'ReadValue':
      return (b'wrong' if self.wrong_identity else b'JXSL-MIC34LE' if path.endswith('/model') else b'JINXUSOLU',)
    if method == 'GetNameOwner':
      return (':1.42',)
    if method == 'StartNotify':
      self.queue.put_nowait(NS(body=(b.GATT, {'Notifying': ('b', True)}, [])))
    if method == 'WriteValue':
      command = bytes(body[0]).decode().strip()
      self.commands.append(command)
      reply = b'ELM327 v2.3\r>' if command == 'ATI' else b'?\r>' if self.setup_failure else b'OK\r>'
      self.queue.put_nowait(notification(reply))
    return ()


def test_only_allowlisted_device_identity_and_setup():
  async def check():
    link = DiscoveryLink()
    await link.connect()
    assert link.commands == ['ATI', *b.SETUP]
    await link.close()
    assert {p for p, m in link.calls if m in ('Connect', 'Disconnect')} == {link.target}
    assert len(link.rules) == 2  # UART bytes and actual device disconnects.
    for option in ('wrong_identity', 'setup_failure'):
      failed = DiscoveryLink(**{option: True})
      with pytest.raises(ValueError):
        await failed.connect()
      await failed.close()
      assert '220101' not in failed.commands
  asyncio.run(check())


def test_run_reconnect_clears_snapshot_and_does_not_mix_sessions(monkeypatch):
  from contextlib import asynccontextmanager
  import jeepney.io.asyncio
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': ['max_temp_c']}
  snapshots, sessions = [], []
  @asynccontextmanager
  async def router(*args):
    yield None
  class Session(FakeLink):
    def __init__(self, _router, address):
      super().__init__([])
      self.index = len(sessions)
      self.closed = False
      sessions.append(self)
    async def connect(self):
      pass
    async def command(self, command):
      if self.index == 0:
        raise TimeoutError('synthetic partial response')
      return response({'220101': payload(), '220105': soc_payload(), '220106': coolant_payload()}[command])
    async def close(self):
      self.closed = True
  async def sleep(delay):
    if 0 <= delay <= b.POWER_PERIOD:
      raise asyncio.CancelledError
    assert delay == 5.0
    assert len(sessions) <= 2, 'unexpected retry loop in synthetic transport'
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'Link', Session)
  monkeypatch.setattr(b, 'vehicle_monitor', lambda: NS(update=lambda _: None))
  monkeypatch.setattr(b, 'read_json', lambda _: config)
  monkeypatch.setattr(b, 'write_snapshot', lambda *args, **kwargs: snapshots.append(args))
  monkeypatch.setattr(b.asyncio, 'sleep', sleep)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert [x[0] for x in snapshots] == ['connecting', 'disconnected', 'unavailable', 'connecting',
                                      'receiving', 'disconnected']
  assert all(s.closed for s in sessions) and len(sessions) == 2








def test_hud_reads_ram_without_opening_bluetooth_or_can(tmp_path, monkeypatch):
  from tools.jetlink.hud_battery import BatteryPublisher  # noqa: TID251
  config_path, snapshot_path = tmp_path / 'config', tmp_path / 'snapshot'
  values = {'max_temp_c': 28, 'min_temp_c': 25, 'soc_percent': 75.5, 'coolant_inlet_c': 27,
            'thermal_mode': 'cooling', 'temp_change_5min_c': -2}
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': sorted(values)}
  config_path.write_text(json.dumps(config))
  monkeypatch.setattr(b, 'CONFIG', config_path)
  monkeypatch.setattr(b, 'SNAPSHOT', snapshot_path)
  now = b.time.monotonic()
  b.write_snapshot('receiving', values, frozenset(values), snapshot_path,
                   b.config_id(b.configuration(config)))
  publisher = BatteryPublisher(tmp_path / 'no_passive_profile')
  packet = publisher.update('KIA_PV5', True, now + .1)
  assert {k: v['value'] for k, v in packet['fields'].items()} == values
  assert publisher.socket is None and publisher.messaging is None
  assert publisher.update('KIA_PV5', True, now + 6)['fields']['max_temp_c']['value'] == 28
  config_path.write_text(json.dumps(config | {'confirmed_fields': []}))
  assert not publisher.update('KIA_PV5', True, now + 7)['fields']
  assert publisher.update('KIA_PV5', False, now + 8) is None


def test_manager_gate_offroad_wrong_car_and_opt_in():
  # Isolate the actual predicate without importing native platform bindings.
  tree = ast.parse(Path('openpilot/system/manager/process_config.py').read_text())
  fn = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'obd_battery')
  fn.returns = None
  for arg in fn.args.args:
    arg.annotation = None
  env = {'os': NS(path=NS(isfile=lambda _: True))}
  exec(compile(ast.Module(body=[fn], type_ignores=[]), '<gate>', 'exec'), env)
  check = env['obd_battery']
  cp = NS(notCar=False, carFingerprint='KIA_PV5')
  assert check(True, None, cp)
  assert not check(False, None, cp)
  assert not check(True, None, NS(notCar=True, carFingerprint='KIA_PV5'))
  assert not check(True, None, NS(notCar=False, carFingerprint='OTHER'))
  env['os'].path.isfile = lambda _: False
  assert not check(True, None, cp)


def test_provisioning_preserves_existing_target_and_requires_calibration(tmp_path, monkeypatch, capsys):
  import sys
  from tools.jetlink import configure_obd_battery as config_tool  # noqa: TID251
  config_path = tmp_path / 'private' / 'config.json'
  monkeypatch.setattr(config_tool, 'CONFIG', config_path)
  def command(*args):
    monkeypatch.setattr(sys, 'argv', ['configure_obd_battery', *args])
    config_tool.main()
  command('enable', '--address', '00:11:22:33:44:55')
  original = config_path.read_bytes()
  assert b.configuration(json.loads(original))[1] == frozenset()
  with pytest.raises(SystemExit):
    command('enable', '--address', '00:AA:BB:CC:DD:EE')
  assert config_path.read_bytes() == original
  with pytest.raises(SystemExit):
    command('confirm', '--fields', 'max_temp_c')
  assert config_path.read_bytes() == original
  command('confirm', '--fields', 'max_temp_c', '--compared-on-pv5')
  assert b.configuration(b.read_json(config_path))[1] == frozenset(['max_temp_c'])
  command('disable')
  assert not config_path.exists()


def test_notify_cleanup_failure_still_disconnects_only_target():
  class CleanupLink(DiscoveryLink):
    async def call(self, path, interface, method, *args, **kwargs):
      if method == 'StopNotify':
        assert kwargs['timeout'] == 0.5
        raise TimeoutError('synthetic stuck notification cleanup')
      return await super().call(path, interface, method, *args, **kwargs)
  async def check():
    link = CleanupLink()
    await link.connect()
    await link.close()
    assert [(p, m) for p, m in link.calls if m == 'Disconnect'] == [(link.target, 'Disconnect')]
  asyncio.run(check())
