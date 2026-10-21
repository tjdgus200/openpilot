import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from host_health import thermal_health
from openpilot.common import jetlink_status as status


def test_storage_recovery_is_not_reported_as_heat_or_legacy_protection(tmp_path):
  from host_health import storage_health

  marker, state = tmp_path / 'marker', tmp_path / 'state'
  value = {'severity': 'ok', 'reason': ''}
  storage_health(value, state, marker)
  assert value['storage_mode'] == 'legacy-writable' and value['severity'] == 'ok'
  marker.write_text('{}')
  for mode in ('protected', 'base-recovery'):
    state.write_text(json.dumps({'state': mode, 'system_read_only': True}))
    value = {'severity': 'ok', 'reason': ''}
    storage_health(value, state, marker)
    assert value['severity'] == ('ok' if mode == 'protected' else 'error')
    assert value['storage_mode'] == mode
  state.write_text('{torn')
  value = {'severity': 'error', 'reason': 'thermal fault'}
  storage_health(value, state, marker)
  assert value['storage_mode'] == 'unknown'
  assert value['reason'].startswith('thermal fault; ')


def test_thermal_uses_zone_limits_and_ignores_disconnected_sensors(tmp_path):
  zone = tmp_path / 'thermal_zone0'
  zone.mkdir()
  (zone / 'type').write_text('tj-thermal')
  (zone / 'trip_point_0_type').write_text('passive')
  (zone / 'trip_point_0_temp').write_text('92000')
  for temp, severity in ((86000, 'ok'), (87000, 'warning'), (92000, 'error'), (60000, 'ok'), (-256000, 'unknown')):
    (zone / 'temp').write_text(str(temp))
    assert thermal_health(tmp_path)['severity'] == severity


def test_expiry_and_error_override_active_model(tmp_path, monkeypatch):
  link, model = tmp_path / 'link', tmp_path / 'model'
  monkeypatch.setattr(status, 'LINK_STATUS', link)
  monkeypatch.setattr(status, 'MODEL_STATUS', model)
  monkeypatch.setattr(status.time, 'monotonic', lambda: 20.0)
  model.write_text(json.dumps({'updated': 20.0, 'active': True}))
  record = {
    'updated': 20.0,
    'state': 'ready',
    'peer': {'carrot_host': 'jetson'},
    'telemetry_updated': 19.0,
    'telemetry': {
      'carrot_health': {
        'age_s': 0.1,
        'severity': 'warning',
        'temp_c': 87.0,
        'addresses': [{'address': '192.168.0.199'}, {'address': '<script>'}, {'address': '127.0.0.1'}],
      }
    },
  }
  link.write_text(json.dumps(record))
  assert status.diagnostics()['addresses'] == ['192.168.0.199']
  assert status.badge() == ('jetSON HOT', 'loading')
  model.write_text(json.dumps({'updated': 20.0, 'active': False, 'error': 'inference timeout'}))
  assert status.badge() == ('jetSON ERROR', 'error')
  assert status.diagnostics()['reason'] == 'inference timeout'
  model.write_text(json.dumps({'updated': 20.0, 'active': True, 'error': ''}))
  record['telemetry_updated'] = 10.0
  link.write_text(json.dumps(record))
  assert status.diagnostics()['addresses'] == []
  assert status.diagnostics()['temp_c'] is None
  assert status.badge() == ('jetSON DATA?', 'loading')
  record.update(state='retrying', error='engine failed')
  link.write_text(json.dumps(record))
  assert status.badge() == ('jetSON ERROR', 'error')


def test_hello_telemetry_never_becomes_current_health(tmp_path, monkeypatch):
  link = tmp_path / 'link'
  monkeypatch.setattr(status, 'LINK_STATUS', link)
  monkeypatch.setattr(status, 'MODEL_STATUS', tmp_path / 'missing')
  monkeypatch.setattr(status.time, 'monotonic', lambda: 10.0)
  link.write_text(json.dumps({'updated': 10.0, 'state': 'ready', 'peer': {'carrot_host': 'jetson', 'telemetry': {'temp_c': 42}}}))
  assert status.diagnostics()['temp_c'] is None
  monkeypatch.setattr(status.time, 'monotonic', lambda: 15.0)
  assert status.badge() == ('jetSON ERROR', 'error')


def power_sensor(root, number=0, rail='VDD_IN', voltage='5048', current='1520', name='ina3221'):
  monitor = root / f'hwmon{number}'
  monitor.mkdir()
  (monitor / 'name').write_text(name)
  for channel in (1, 2, 3):
    (monitor / f'in{channel}_label').write_text(rail if channel == 1 else 'VDD_SOC')
    (monitor / f'in{channel}_input').write_text(voltage)
    (monitor / f'curr{channel}_input').write_text(current)
  return monitor


def test_total_module_power_uses_millivolts_times_milliamps_without_summing(tmp_path):
  from host_health import module_power

  sensor = power_sensor(tmp_path)
  assert abs(module_power(tmp_path) - 7.67296) < 1e-9
  # CPU/GPU/SOC readings overlap VDD_IN and must never be added to it.
  (sensor / 'curr2_input').write_text('999999')
  assert abs(module_power(tmp_path) - 7.67296) < 1e-9
  (sensor / 'curr1_input').write_text('0')
  assert module_power(tmp_path) == 0.0


def test_power_missing_ambiguous_or_invalid_is_unavailable_not_guessed(tmp_path):
  from host_health import module_power

  assert module_power(tmp_path) is None
  sensor = power_sensor(tmp_path, rail='VDD_CPU_GPU_CV')
  assert module_power(tmp_path) is None
  (sensor / 'in1_label').write_text('VDD_IN')
  for voltage, current in [('0', '1000'), ('5000', '-1'), ('nan', '1000'), ('5000', 'inf'), ('60000', '100000')]:
    (sensor / 'in1_input').write_text(voltage)
    (sensor / 'curr1_input').write_text(current)
    assert module_power(tmp_path) is None
  (sensor / 'in1_input').write_text('5000')
  (sensor / 'curr1_input').write_text('1000')
  power_sensor(tmp_path, number=1)
  assert module_power(tmp_path) is None
  # A non-INA sensor with the same label cannot become our power source.
  (sensor / 'name').write_text('unrelated')
  assert abs(module_power(tmp_path) - 7.67296) < 1e-9


def test_power_half_second_cadence_preserves_slow_sensor_network_rate(tmp_path, monkeypatch):
  import host_health as h

  clock = [0.0]
  counts = {'thermal': 0, 'storage': 0, 'network': 0, 'power': 0}
  rows, sleeps = [], []

  def thermal():
    counts['thermal'] += 1
    clock[0] += 0.07
    return {'temp_c': 40 + counts['thermal']}

  def storage(value):
    counts['storage'] += 1

  def network():
    counts['network'] += 1
    return []

  def power():
    counts['power'] += 1
    clock[0] += 0.002
    return float(counts['power'])

  def sleep(duration):
    sleeps.append(duration)
    rows.append(json.loads((tmp_path / 'health.json').read_text()))
    clock[0] += duration
    if len(rows) == 4:
      raise StopIteration

  monkeypatch.setattr(h, 'STATUS', tmp_path / 'health.json')
  monkeypatch.setattr(h, 'thermal_health', thermal)
  monkeypatch.setattr(h, 'storage_health', storage)
  monkeypatch.setattr(h, 'network_addresses', network)
  monkeypatch.setattr(h, 'module_power', power)
  monkeypatch.setattr(h.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(h.time, 'sleep', sleep)
  worker = object.__new__(h.HostHealth)
  worker.error = None
  worker.snapshot = {}
  import pytest

  with pytest.raises(StopIteration):
    worker._run()
  assert counts == {'thermal': 2, 'storage': 2, 'network': 2, 'power': 4}
  assert [row['updated'] for row in rows] == [0.0, 0.5, 1.0, 1.5]
  assert [row['power_w'] for row in rows] == [1.0, 2.0, 3.0, 4.0]
  assert rows[0]['temp_c'] == rows[1]['temp_c'] and rows[2]['temp_c'] == rows[3]['temp_c']
  assert sleeps[0] < sleeps[1] and all(0.05 <= delay <= 0.5 for delay in sleeps)


def test_sensor_overrun_keeps_a_sleep_instead_of_busy_catch_up(tmp_path, monkeypatch):
  import host_health as h
  import pytest

  clock = [0.0]
  sleeps = []

  def power():
    clock[0] += 1.2
    return 7.0

  def sleep(delay):
    sleeps.append(delay)
    raise StopIteration

  monkeypatch.setattr(h, 'STATUS', tmp_path / 'health.json')
  monkeypatch.setattr(h, 'thermal_health', dict)
  monkeypatch.setattr(h, 'storage_health', lambda value: None)
  monkeypatch.setattr(h, 'network_addresses', list)
  monkeypatch.setattr(h, 'module_power', power)
  monkeypatch.setattr(h.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(h.time, 'sleep', sleep)
  worker = object.__new__(h.HostHealth)
  worker.error = None
  worker.snapshot = {}
  with pytest.raises(StopIteration):
    worker._run()
  assert sleeps == [0.05]
