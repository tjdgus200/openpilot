"""A snapshot replaced during file IO must be checked against the post-read clock."""
import ast
from pathlib import Path

import pytest

from openpilot.common import obd_status
from openpilot.selfdrive.carrot import obd_battery as b
from hud_battery import BatteryPublisher


@pytest.fixture
def concurrent_snapshot(tmp_path, monkeypatch):
  config_path, snapshot_path = tmp_path / 'config', tmp_path / 'snapshot'
  config_path.write_text('{}')
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': ['pack_power_kw']}
  identity = b.config_id(b.configuration(config))
  clock = [100.]
  record = {'version': 1, 'state': 'receiving', 'config_id': identity, 'updated': 100.002,
            'metrics': {'pack_power_kw': 1.}, 'field_times': {'pack_power_kw': 100.002}}
  def read(path):
    if path == config_path:
      return config
    assert path == snapshot_path
    clock[0] = 100.003  # The atomic writer published after update()'s initial time.
    return record
  monkeypatch.setattr(b, 'CONFIG', config_path)
  monkeypatch.setattr(b, 'SNAPSHOT', snapshot_path)
  monkeypatch.setattr(b, 'read_json', read)
  monkeypatch.setattr(obd_status, 'read_json', read)
  return config_path, snapshot_path, clock, record


def test_pre_read_clock_reproduces_badge_false_negative(concurrent_snapshot):
  config, snapshot, _, _ = concurrent_snapshot
  # The legacy explicit-now API reproduces the missed race. Production must
  # supply a monotonic callback that samples after the file reads instead.
  assert not obd_status.ObdBadgeStatus(config, snapshot).update(100., True)


@pytest.mark.parametrize('stamp,expected', [(100.002, True), (95.002, False), (100.004, False)])
def test_badge_uses_post_read_time_without_extending_freshness(concurrent_snapshot, stamp, expected):
  config, snapshot, clock, record = concurrent_snapshot
  record['updated'] = stamp
  status = obd_status.ObdBadgeStatus(config, snapshot)
  assert status.update(100., True, clock=lambda: clock[0]) is expected


@pytest.mark.parametrize('stamp,expected', [(100.002, True), (95.002, False), (100.004, False)])
def test_hud_uses_post_read_time_for_ble_values(concurrent_snapshot, tmp_path, stamp, expected):
  _, _, clock, record = concurrent_snapshot
  record['updated'] = record['field_times']['pack_power_kw'] = stamp
  publisher = BatteryPublisher(tmp_path / 'no_passive_profile')
  packet = publisher.update('KIA_PV5', True, 100., clock=lambda: clock[0])
  assert ('pack_power_kw' in packet['fields']) is expected
  if expected:
    assert packet['fields']['pack_power_kw']['age_s'] == pytest.approx(.001)
    assert packet['sampled_at'] == 100.003
  assert publisher.socket is None and publisher.messaging is None


@pytest.mark.parametrize('path,attribute', [
  ('openpilot/selfdrive/ui/ui_state.py', '_obd_status'), ('tools/jetlink/hud_protocol.py', 'battery'),
])
def test_production_callers_pass_the_monotonic_clock(path, attribute):
  tree = ast.parse(Path(path).read_text(encoding='utf-8'))
  calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == 'update'
           and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == attribute]
  assert len(calls) == 1
  assert any(kw.arg == 'clock' and isinstance(kw.value, ast.Attribute)
             and isinstance(kw.value.value, ast.Name) and kw.value.value.id == 'time'
             and kw.value.attr == 'monotonic' for kw in calls[0].keywords)
