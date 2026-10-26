"""Fault-injected sequential scheduling, not live adapter timing evidence."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.carrot import obd_battery as b
from test_obd_battery import response, payload, soc_payload, coolant_payload


def test_power_two_hz_slow_fields_one_hz_keep_own_ages(monkeypatch, tmp_path):
  clock = [100.0]
  monkeypatch.setattr(b, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
  commands = []
  class Link:
    async def command(self, command):
      commands.append((clock[0], command))
      return response({'220101': payload(), '220105': soc_payload(), '220106': coolant_payload()}[command])
  async def check():
    poller = b.MetricPoller()
    for frame in range(8):
      clock[0] = 100.0 + frame * .5
      observed, values, stamps = await poller.poll(Link())
      assert 'temp_change_5min_c' not in values
      assert 'thermal_mode' not in values
      assert stamps['pack_power_kw'] == clock[0]
      assert stamps['max_temp_c'] == 100 + frame // 2
      assert stamps['soc_percent'] == 100 + frame // 2
      assert observed == min(stamps.values())
      path = tmp_path / 'snapshot'
      b.write_snapshot('receiving', values, frozenset(values), path, 'cfg', observed_at=observed, field_times=stamps)
      fields = b.snapshot_packet(b.read_json(path), clock[0], 'cfg')['fields']
      assert fields['pack_power_kw']['age_s'] == 0
      assert fields['max_temp_c']['age_s'] == frame % 2 * .5
    assert [c for _, c in commands].count('220101') == 8
    assert [c for _, c in commands].count('220105') == 4
    assert [c for _, c in commands].count('220106') == 4
    # No growing history or stale schedule catch-up after a long wait.
    assert not hasattr(poller, 'history')
    clock[0] += 10
    await poller.poll(Link())
    assert len(commands) == 19
    assert len(poller.values) <= len(b.FIELDS)
  asyncio.run(check())


@pytest.mark.parametrize('fault', ['connect', 'poll', 'immediate'])
def test_unlimited_retries_keep_attempt_deadlines_cleanup_and_cancellation(monkeypatch, fault):
  import jeepney.io.asyncio
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  sessions, states = [], []
  @asynccontextmanager
  async def router(*args):
    yield None
  class Session:
    def __init__(self, router, address):
      assert all(s.closed for s in sessions)
      self.closed = False
      sessions.append(self)
    async def connect(self):
      if fault == 'connect':
        await asyncio.Event().wait()
      if fault == 'immediate':
        raise ConnectionError
    async def close(self):
      self.closed = True
  class Poller:
    async def poll(self, link, **kwargs):
      await asyncio.Event().wait()
  real_sleep = asyncio.sleep
  delays = []
  async def sleep(delay):
    delays.append(delay)
    assert delay == 5.0
    assert all(s.closed for s in sessions)
    if len(delays) == 4:
      raise asyncio.CancelledError
    await real_sleep(0)
  monkeypatch.setattr(jeepney.io.asyncio, 'open_dbus_router', router)
  monkeypatch.setattr(b, 'Link', Session)
  monkeypatch.setattr(b, 'MetricPoller', Poller)
  monkeypatch.setattr(b, 'vehicle_monitor', lambda: SimpleNamespace(update=lambda _: None))
  monkeypatch.setattr(b, 'read_json', lambda _: config)
  monkeypatch.setattr(b, 'ATTEMPT_TIMEOUT', .01)
  monkeypatch.setattr(b, 'write_snapshot', lambda state, **kwargs: states.append(state))
  monkeypatch.setattr(b.asyncio, 'sleep', sleep)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert len(sessions) == 4 and all(s.closed for s in sessions)
  assert states.count('connecting') == 4
  assert 'retry_exhausted' not in states and 'receiving' not in states


def test_snapshot_rejects_missing_bad_or_expired_individual_timestamps():
  record = {'version': 1, 'updated': 100., 'state': 'receiving', 'config_id': 'cfg',
            'metrics': {'pack_power_kw': -5., 'soc_percent': 70.},
            'field_times': {'pack_power_kw': 104.5, 'soc_percent': 95.}}
  fields = b.snapshot_packet(record, 105., 'cfg')['fields']
  assert fields == {'pack_power_kw': {'value': -5., 'age_s': .5}}
  for invalid in (None, True, float('nan'), 106.):
    record['field_times']['pack_power_kw'] = invalid
    assert not b.snapshot_packet(record, 105., 'cfg')['fields']


def test_ram_publisher_refreshes_power_at_half_second_without_new_transport(tmp_path, monkeypatch):
  import json
  from hud_battery import BatteryPublisher
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': ['pack_power_kw']}
  cfg, snap = tmp_path / 'config', tmp_path / 'snapshot'
  cfg.write_text(json.dumps(config))
  monkeypatch.setattr(b, 'CONFIG', cfg)
  monkeypatch.setattr(b, 'SNAPSHOT', snap)
  identity = b.config_id(b.configuration(config))
  publisher = BatteryPublisher(tmp_path / 'no_profile')
  for frame, power in enumerate((10., -5., 0.)):
    now = 100. + frame * .5
    b.write_snapshot('receiving', {'pack_power_kw': power}, frozenset(['pack_power_kw']), snap,
                     identity, observed_at=now, field_times={'pack_power_kw': now})
    packet = publisher.update('KIA_PV5', True, now)
    assert packet['fields']['pack_power_kw']['value'] == power
    assert publisher.socket is None
  assert publisher.update('KIA_PV5', False, 102.) is None
