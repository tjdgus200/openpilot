"""Stationary layout admission only; no vehicle control or OBD traffic."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/jetlink'), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]
from cluster_battery import BatteryPanelData, StationaryBatteryView


@pytest.mark.parametrize('gear', ['D', 'R', 'N', 'B', 'S', 'L'])
@pytest.mark.parametrize('fps', [10, 15, 20, 25, 30])
def test_each_gear_requires_one_continuous_second_at_every_hud_rate(gear, fps):
  view, data = StationaryBatteryView(), BatteryPanelData()
  for i in range(fps):
    assert not view.update(data, gear, 0., True, 100. + i / fps)
  assert view.update(data, gear, 0., True, 101.)
  assert not view.update(data, gear, .001, True, 101.01)  # No rounded-speed admission.
  assert not view.update(data, gear, 0., True, 101.02)


@pytest.mark.parametrize('fault', ['stale', 'gap', 'clock_back', 'nan', 'inf', 'negative', 'boolean'])
def test_invalid_or_missing_motion_cancels_the_dwell(fault):
  view, data = StationaryBatteryView(), BatteryPanelData()
  for i in range(12):
    active = view.update(data, 'D', 0., True, 100. + i / 10)
  assert active
  now, speed, fresh = 101.2, 0., True
  if fault == 'stale':
    fresh = False
  elif fault == 'gap':
    now = 102.
  elif fault == 'clock_back':
    now = 99.
  elif fault == 'boolean':
    speed = False
  else:
    speed = {'nan': float('nan'), 'inf': float('inf'), 'negative': -.001}[fault]
  assert not view.update(data, 'D', speed, fresh, now)
  assert not view.update(data, 'D', 0., True, now + .1)


def test_park_is_immediate_and_duplicate_or_invalid_clocks_cannot_advance_time():
  view, data = StationaryBatteryView(), BatteryPanelData()
  assert view.update(data, 'P', 0., True, 100.)
  assert not view.update(data, 'P', 0., False, 100.1)
  for _ in range(100):
    assert not view.update(data, 'D', 0., True, 100.2)
  assert not view.update(data, 'D', 0., True, float('nan'))
  assert not view.update(data, 'D', 0., True, 100.3)


def test_renderer_stale_snapshot_and_page_changes_reset_the_timer(monkeypatch):
  import hud_health as h
  import hud_protocol
  from cluster_live import standby_state
  clock, stale = [100.], [False]
  def snapshot():
    sent = clock[0] + 1000.
    return clock[0], {'sent': sent, 'received': {'carState': sent - (1. if stale[0] else .01)},
                      'events': {'carState': 'present'}, 'alive': {'carState': True}, 'valid': {'carState': True}}
  monkeypatch.setattr(hud_protocol, 'read_snapshot', snapshot)
  monkeypatch.setattr(h.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(h, '_fresh', lambda *a: {})
  monkeypatch.setattr(h.ClusterUiRenderer, 'render', lambda *a: None)
  ui = h.HealthRenderer(screen_mode=8)
  state = replace(standby_state(), gear_text='D', speed_kph=0.)
  for i in range(12):
    clock[0] = 100. + i / 10
    ui.render(state)
    assert ui._charging_dashboard_active == (i >= 10)
  stale[0], clock[0] = True, 101.2
  ui.render(state)
  assert not ui._charging_dashboard_active
  stale[0], clock[0] = False, 101.3
  ui.render(state)
  assert not ui._charging_dashboard_active
  ui.screen_mode = 0
  ui.render(state)
  assert not ui._charging_dashboard_active
  ui.screen_mode = 8
  clock[0] += .1
  ui.render(state)
  assert not ui._charging_dashboard_active
  ui.render(replace(state, gear_text='P'))
  assert ui._charging_dashboard_active  # Existing P view is preserved.
