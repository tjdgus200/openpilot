import asyncio
import importlib.util
import json
from pathlib import Path
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.selfdrive.carrot import obd_settings as settings
from openpilot.selfdrive.carrot.obd_battery import configuration, config_id
from openpilot.selfdrive.carrot.bluetooth.model import atomic_json

MAC = '00:11:22:33:44:55'
OTHER = '00:11:22:33:44:66'


def test_registration_preserves_confirmations_and_rejects_unknown_or_replacement(tmp_path):
  path = tmp_path / 'obd.json'
  import pytest
  with pytest.raises(ValueError):
    settings.enroll(MAC, [], path)
  settings.enroll(MAC, [{'address': MAC}], path)
  assert settings.saved(path)['confirmed_fields'] == []
  value = settings.saved(path)
  value['confirmed_fields'] = ['soc_percent']
  path.write_text(json.dumps(value))
  settings.enroll(MAC, [], path)
  assert settings.saved(path) == value
  with pytest.raises(ValueError):
    settings.enroll(OTHER, [{'address': OTHER}], path)
  with pytest.raises(ValueError):
    settings.forget(OTHER, path)
  assert settings.saved(path) == value
  settings.forget(MAC, path)
  assert not path.exists()


def test_status_requires_matching_fresh_snapshot_and_keeps_corrupt_config(tmp_path):
  config, snapshot = tmp_path / 'config', tmp_path / 'snapshot'
  settings.enroll(MAC, [{'address': MAC}], config)
  record = {'state': 'receiving', 'updated': 10, 'config_id': config_id(configuration(settings.saved(config)))}
  snapshot.write_text(json.dumps(record))
  assert settings.status(config, snapshot, 11)['fresh']
  assert not settings.status(config, snapshot, 15)['fresh']
  assert not settings.status(config, snapshot, 9)['fresh']
  record['config_id'] = 'old-target'
  snapshot.write_text(json.dumps(record))
  assert not settings.status(config, snapshot, 11)['fresh']
  config.write_text('invalid')
  assert settings.status(config, snapshot, 11)['configError']
  import pytest
  with pytest.raises(ValueError):
    settings.enroll(MAC, [{'address': MAC}], config)
  assert config.read_text() == 'invalid'


def test_obd_http_guard_registration_and_delayed_guard_recheck(tmp_path, monkeypatch):
  spec = importlib.util.spec_from_file_location('obd_feature_test', Path(__file__).parents[2] / 'server/features/bluetooth.py')
  feature = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(feature)
  monkeypatch.setattr(feature, 'RUNTIME', tmp_path)
  monkeypatch.setattr(settings, 'CONFIG', tmp_path / 'obd.json')
  monkeypatch.setattr(settings, 'SNAPSHOT', tmp_path / 'snapshot.json')
  calls = []
  moving = False

  class Client:
    async def snapshot(self):
      if moving:
        atomic_json(tmp_path / 'status.json', {'time': time.monotonic(), 'stationary': False})
      return {'adapters': [{'powered': True}], 'devices': [{'address': MAC, 'name': '<OBD>', 'connected': False}]}
    async def close(self):
      calls.append('close')

  monkeypatch.setattr(feature, 'Bluez', Client)
  async def enabled():
    return True
  monkeypatch.setattr(feature, 'radio_enabled', enabled)

  async def run():
    nonlocal moving
    app = web.Application()
    feature.register(app)
    async with TestClient(TestServer(app)) as client:
      payload = {'address': MAC}
      assert (await client.post('/api/bluetooth/obd-connect', json=payload)).status == 409
      atomic_json(tmp_path / 'status.json', {'time': time.monotonic(), 'stationary': True})
      assert (await client.post('/api/bluetooth/obd-connect', json=payload, headers={'Origin': 'https://evil.example'})).status == 403
      assert (await client.post('/api/bluetooth/obd-connect', data='{}')).status == 415
      assert (await client.post('/api/bluetooth/obd-connect', json={'address': OTHER})).status == 400
      moving = True
      assert (await client.post('/api/bluetooth/obd-connect', json=payload)).status == 409
      assert not settings.CONFIG.exists()
      moving = False
      atomic_json(tmp_path / 'status.json', {'time': time.monotonic(), 'stationary': True})
      assert (await client.post('/api/bluetooth/obd-connect', json=payload)).status == 200
      result = await client.get('/api/bluetooth/obd')
      assert result.headers['Cache-Control'] == 'no-store'
      state = await result.json()
      assert state['obd']['registered'] and not state['obd']['fresh']
      assert state['obd']['state'] == 'waiting'
      assert calls == []  # No second BLE client, pairing, radio reset or reader restart.
      assert (await client.post('/api/bluetooth/obd-forget', json={'address': OTHER})).status == 400
      assert (await client.post('/api/bluetooth/obd-forget', json=payload)).status == 200
      assert not settings.CONFIG.exists()
  asyncio.run(run())
