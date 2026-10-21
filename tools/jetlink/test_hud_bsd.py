from types import SimpleNamespace as NS

import pytest

from hud_bsd import warning_sides


def snapshot():
  return 10.0, {'sent': 100.0, 'received': {'carState': 99.95}, 'valid': {'carState': True}, 'alive': {'carState': True}}


@pytest.mark.parametrize(
  'turn_left,turn_right,bsd_left,bsd_right,expected',
  [
    (True, False, True, False, (True, False)),
    (False, True, False, True, (False, True)),
    (True, False, False, True, (False, False)),
    (False, False, True, True, (False, False)),
    (True, True, True, True, (True, True)),
  ],
)
def test_warning_requires_turn_and_bsd_on_the_same_side(turn_left, turn_right, bsd_left, bsd_right, expected):
  state = NS(left_signal=turn_left, right_signal=turn_right, left_blindspot=bsd_left, right_blindspot=bsd_right)
  assert warning_sides(state, snapshot(), 10.1) == expected


@pytest.mark.parametrize('change', ['missing', 'stale_snapshot', 'stale_car', 'sum_stale', 'invalid', 'not_alive', 'nan', 'bad_type'])
def test_stale_invalid_or_missing_data_never_keeps_a_red_warning(change):
  state = NS(left_signal=True, right_signal=True, left_blindspot=True, right_blindspot=True)
  stamp, data = snapshot()
  now = 10.1
  if change == 'missing':
    assert warning_sides(state, None, now) == (False, False)
    return
  if change == 'stale_snapshot':
    now = 10.4
  elif change == 'stale_car':
    data['received']['carState'] = 99.6
  elif change == 'sum_stale':
    data['received']['carState'] = 99.75
  elif change == 'invalid':
    data['valid']['carState'] = False
  elif change == 'not_alive':
    data['alive']['carState'] = False
  elif change == 'nan':
    data['sent'] = float('nan')
  elif change == 'bad_type':
    data['sent'] = '100'
  assert warning_sides(state, (stamp, data), now) == (False, False)


def test_two_hz_pulse_starts_bright_and_never_disappears():
  from hud_bsd import edge_alpha, DIM_ALPHA

  assert [edge_alpha(10 + i / 10, 10) for i in range(10)] == [255, 255, 255, DIM_ALPHA, DIM_ALPHA, 255, 255, 255, DIM_ALPHA, DIM_ALPHA]
  assert edge_alpha(9, 10) == 255
  assert edge_alpha(float('nan'), 10) == 255


def test_official_visual_bsd_augments_vehicle_bsd_before_hud_warning():
  from openpilot.selfdrive.carrot.xiaoge.xiaoge_vision import XiaogeVisionResult, apply_xiaoge_vision_result

  car = NS(leftLaneLine=0, rightLaneLine=0, leftBlindspot=True, rightBlindspot=False)
  vision = XiaogeVisionResult(-1, -1, False, 0, False, True, True, 100_000_000_000)
  apply_xiaoge_vision_result(car, vision, 100_100_000_000)
  state = NS(left_signal=True, right_signal=True,
             left_blindspot=car.leftBlindspot, right_blindspot=car.rightBlindspot)
  assert warning_sides(state, snapshot(), 10.1) == (True, True)
  assert car.leftBlindspot is True  # An ONNX clear never erases a native warning.


@pytest.mark.parametrize('layout', [0, 1])
def test_warning_uses_physical_outer_thirds_and_vehicle_alerts_stay_last(monkeypatch, layout):
  import hud_health as hud

  renderer = object.__new__(hud.HealthRenderer)
  renderer.width, renderer.height, renderer.language, renderer.panel_layout = 1920, 480, 'ko', layout
  renderer.host_health, renderer.host_badge_drawn = {'severity': 'ok'}, True
  renderer._bsd_warning, renderer._bsd_started = (True, True), (10.0, 10.0)
  calls = []
  monkeypatch.setattr(hud.time, 'monotonic', lambda: 10.0)
  for name in ('rl_push_matrix', 'rl_pop_matrix', 'rl_scalef'):
    monkeypatch.setattr(hud.rl, name, lambda *a: None)
  monkeypatch.setattr(hud.rl, 'draw_rectangle_gradient_h', lambda *a: calls.append(('gradient', a)))
  monkeypatch.setattr(hud.rl, 'draw_rectangle', lambda *a: calls.append(('edge', a)))
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_alert_overlay', lambda *a: calls.append(('vehicle', ())))
  renderer._draw_alert_overlay(NS(size=3, text1='TAKE OVER', text2=''))
  gradients = [a for kind, a in calls if kind == 'gradient']
  assert [a[:4] for a in gradients] == [(0, 32, 640, 448), (1280, 32, 640, 448)]
  edges = [a[0] for kind, a in calls if kind == 'edge' and a[2] == 14]
  assert edges == [7, 1899]
  assert calls[-1][0] == 'vehicle'
