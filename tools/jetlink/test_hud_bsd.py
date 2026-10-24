from types import SimpleNamespace as NS

import pytest

from hud_bsd import warning_sides


def snapshot():
  return 10.0, {'sent': 100.0, 'received': {'carState': 99.95}, 'valid': {'carState': True}, 'alive': {'carState': True}}


@pytest.mark.parametrize('turn_left,turn_right', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('bsd_left,bsd_right', [(False, False), (True, False), (False, True), (True, True)])
def test_warning_follows_bsd_without_turn_signal_gate(turn_left, turn_right, bsd_left, bsd_right):
  state = NS(left_signal=turn_left, right_signal=turn_right, left_blindspot=bsd_left, right_blindspot=bsd_right)
  assert warning_sides(state, snapshot(), 10.1) == (bsd_left, bsd_right)


def test_warning_does_not_require_signal_or_onnx_fields():
  assert warning_sides(NS(left_blindspot=True, right_blindspot=False), snapshot(), 10.1) == (True, False)


@pytest.mark.parametrize('left,right', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('cached_age,build_delay', [(0., .02), (.35, .02), (0., .35)])
def test_live_snapshot_timestamp_keeps_fresh_bsd_and_rejects_stale_data(monkeypatch, left, right,
                                                                     cached_age, build_delay):
  """SubMaster stamps received data inside update(), after packet() starts."""
  import base64
  import json
  from openpilot.cereal import log
  import openpilot.common.jetlink_status as status
  import hud_protocol as protocol

  clock = NS(now=100.)
  event = log.Event.new_message(carState={'leftBlindspot': left, 'rightBlindspot': right})

  class SubMaster:
    updated = dict.fromkeys(protocol.SERVICES, False)
    updated['carState'] = True
    valid = alive = {'carState': True}
    logMonoTime = {'carState': 100_000_000_000}
    recv_time = {}

    def update(self, timeout):
      clock.now += .003
      self.recv_time['carState'] = clock.now - cached_age

    def __getitem__(self, key):
      assert key == 'carState'
      return event.carState

  builder = object.__new__(protocol.SnapshotBuilder)
  builder.sm = SubMaster()
  builder.next_send, builder.next_params = 0., 200.
  builder.cached, builder.settings = {}, {}
  builder.host_release = builder.nav_map = None
  builder.network = NS(address=lambda: '')

  def camera_previews():
    clock.now += build_delay
    return {}

  builder.camera_previews = camera_previews
  builder.battery_snapshot = lambda now: None
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: clock.now)
  monkeypatch.setattr(status, 'badge', lambda: None)
  record = json.loads(builder.packet())
  assert record['sent'] >= record['received']['carState']
  assert record['sent'] == clock.now
  assert builder.next_send == pytest.approx(100.1)  # Existing 10 Hz cadence.
  with log.Event.from_bytes(base64.b64decode(record['events']['carState'])) as decoded:
    state = NS(left_blindspot=decoded.carState.leftBlindspot, right_blindspot=decoded.carState.rightBlindspot)
    expected = (left, right) if cached_age + build_delay + .05 <= .3 else (False, False)
    assert warning_sides(state, (10., record), 10.05) == expected


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


@pytest.mark.parametrize('signals', [(False, False), (True, False), (False, True), (True, True)])
def test_two_point_five_hz_at_ten_fps_and_steady_without_same_side_signal(signals):
  from hud_bsd import WarningPulse
  pulse = WarningPulse()
  frames = [pulse.update((True, True), signals) for _ in range(100)]
  for side, signal in enumerate(signals):
    pattern = [255, 255, 0, 0]
    values = [frame[side] for frame in frames]
    assert values == (pattern * 25 if signal else [255] * 100)
    if signal:
      assert sum(value == 255 and (i == 0 or values[i - 1] == 0)
                 for i, value in enumerate(values)) == 25
  assert all(0 <= phase < 4 for phase in pulse.phase)
  # Turn cancellation immediately restores the steady warning, even in off phase.
  assert pulse.update((True, True), (False, False)) == (255, 255)
  assert pulse.update((True, True), signals) == (255, 255)
  assert pulse.update((False, False), signals) == (0, 0)
  assert pulse.update((True, True), signals) == (255, 255)


def test_render_uses_turn_request_not_lamp_phase_and_resets_on_stale(monkeypatch):
  import hud_health as hud
  import hud_protocol
  renderer = object.__new__(hud.HealthRenderer)
  renderer.screen_mode = 0
  state = NS(left_signal=True, right_signal=False, left_blindspot=True, right_blindspot=True)
  current = [snapshot()]
  monkeypatch.setattr(hud_protocol, 'read_snapshot', lambda: current[0])
  monkeypatch.setattr(hud.time, 'monotonic', lambda: 10.1)
  monkeypatch.setattr(hud, '_fresh', lambda *args: {})
  outputs = []
  monkeypatch.setattr(hud.ClusterUiRenderer, 'render', lambda *args: outputs.append(renderer._bsd_alpha))
  # Arrow lamp brightness is deliberately opposite; request state owns flashing.
  for _ in range(4):
    renderer.render(state, (False, True))
  assert outputs == [(255, 255), (255, 255), (0, 255), (0, 255)]
  current[0] = None
  renderer.render(state)
  assert outputs[-1] == (0, 0)
  current[0] = snapshot()
  renderer.render(state)
  assert outputs[-1] == (255, 255)
  state.left_signal = False
  renderer.render(state)
  assert outputs[-1] == (255, 255)


def test_official_visual_bsd_augments_vehicle_bsd_before_hud_warning():
  from openpilot.selfdrive.carrot.xiaoge.xiaoge_vision import XiaogeVisionResult, apply_xiaoge_vision_result

  car = NS(leftLaneLine=0, rightLaneLine=0, leftBlindspot=True, rightBlindspot=False)
  vision = XiaogeVisionResult(-1, -1, False, 0, False, True, True, 100_000_000_000)
  apply_xiaoge_vision_result(car, vision, 100_100_000_000)
  state = NS(left_signal=False, right_signal=False,
             left_blindspot=car.leftBlindspot, right_blindspot=car.rightBlindspot)
  assert warning_sides(state, snapshot(), 10.1) == (True, True)
  assert car.leftBlindspot is True  # An ONNX clear never erases a native warning.


@pytest.mark.parametrize('oem_left,oem_right', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('vision_left,vision_right', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('vision_state', ['missing', 'invalid', 'stale', 'fresh'])
def test_vehicle_or_fresh_local_bsd_survives_usb_hud_snapshot(tmp_path, monkeypatch,
                                                           oem_left, oem_right, vision_left, vision_right, vision_state):
  import base64
  import json
  from openpilot.cereal import log
  from openpilot.selfdrive.carrot.xiaoge.xiaoge_vision import XiaogeVisionResult, apply_xiaoge_vision_result
  import hud_protocol as protocol
  from openpilot.selfdrive.modeld.jetlink.daemon import publish_hud

  event = log.Event.new_message(carState={
    'leftBlindspot': oem_left, 'rightBlindspot': oem_right, 'leftBlinker': False, 'rightBlinker': False,
  })
  vision = (None if vision_state == 'missing' else
            XiaogeVisionResult(-1, -1, False, 0, vision_left, vision_right,
                               vision_state != 'invalid', 100_000_000_000))
  apply_xiaoge_vision_result(event.carState, vision, 102_000_000_000 if vision_state == 'stale' else 100_100_000_000)
  _, record = snapshot()
  record.update(version=1, events={'carState': base64.b64encode(event.to_bytes()).decode()})
  publisher = object.__new__(protocol.Publisher)
  publisher.path, publisher.last_sent = tmp_path / 'hud', 0.
  publisher.media_socket = None
  publisher.path.write_bytes(protocol.HEADER.pack(10.) + protocol.encode_snapshot(record))
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: 10.1)
  packets = []
  client = NS(_next_seq=lambda: 1, t=NS(send=lambda kind, seq, parts: packets.append((kind, b''.join(parts)))))
  publish_hud(client, publisher)
  assert len(packets) == 1 and packets[0][0] == protocol.HUD_MESSAGE
  received = json.loads(packets[0][1])
  with log.Event.from_bytes(base64.b64decode(received['events']['carState'])) as decoded:
    state = NS(left_signal=decoded.carState.leftBlinker, right_signal=decoded.carState.rightBlinker,
               left_blindspot=decoded.carState.leftBlindspot, right_blindspot=decoded.carState.rightBlindspot)
    assert warning_sides(state, (10., received), 10.1) == (
      oem_left or (vision_state == 'fresh' and vision_left), oem_right or (vision_state == 'fresh' and vision_right))
  # No second BSD channel or repeat of the same HUD snapshot.
  publish_hud(client, publisher)
  assert len(packets) == 1


@pytest.mark.parametrize('layout', [0, 1])
@pytest.mark.parametrize('sides', [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize('now,alpha', [(10.0, 255), (10.1, 0)])
def test_warning_uses_physical_outer_thirds_and_vehicle_alerts_stay_last(monkeypatch, layout, sides, now, alpha):
  import hud_health as hud

  renderer = object.__new__(hud.HealthRenderer)
  renderer.width, renderer.height, renderer.language, renderer.panel_layout = 1920, 480, 'ko', layout
  renderer.host_health, renderer.host_badge_drawn = {'severity': 'ok'}, True
  renderer._bsd_warning, renderer._bsd_alpha = sides, (alpha, alpha)
  calls = []
  monkeypatch.setattr(hud.time, 'monotonic', lambda: now)
  for name in ('rl_push_matrix', 'rl_pop_matrix', 'rl_scalef'):
    monkeypatch.setattr(hud.rl, name, lambda *a: None)
  monkeypatch.setattr(hud.rl, 'rl_draw_render_batch_active', lambda: calls.append(('batch', ())))
  monkeypatch.setattr(hud.rl, 'draw_rectangle_gradient_h', lambda *a: calls.append(('gradient', a)))
  monkeypatch.setattr(hud.rl, 'draw_rectangle', lambda *a: calls.append(('edge', a)))
  monkeypatch.setattr(hud.rl, 'draw_triangle_fan', lambda points, count, color:
                      calls.append(('corner', ([(points[i].x, points[i].y) for i in range(count)], color))))
  monkeypatch.setattr(hud.ClusterUiRenderer, '_draw_alert_overlay', lambda *a: calls.append(('vehicle', ())))
  renderer._draw_alert_overlay(NS(size=3, text1='TAKE OVER', text2=''))
  assert (calls[0][0] == 'batch') == any(sides)
  sides = tuple(active and alpha > 0 for active in sides)
  gradients = [a for kind, a in calls if kind == 'gradient' and a[3] == 480]
  assert [a[:4] for a in gradients] == [rect for rect, active in zip([(0, 0, 640, 480), (1280, 0, 640, 480)], sides) if active]
  assert [(a[4].a, a[5].a) for a in gradients] == [alpha for alpha, active in zip([(96, 0), (0, 96)], sides) if active]
  edges = [a[:4] for kind, a in calls if kind == 'edge' and a[2] == 24]
  assert edges == [rect for rect, active in zip([(0, 72, 24, 336), (1896, 72, 24, 336)], sides) if active]
  rails = [a[:4] for kind, a in calls if kind == 'gradient' and a[3] == 24]
  expected = ([(72, 0, 568, 24), (72, 456, 568, 24)] if sides[0] else [])
  expected += ([(1280, 0, 568, 24), (1280, 456, 568, 24)] if sides[1] else [])
  assert rails == expected
  assert not any(kind == 'edge' and a[3] in (24, 48) for kind, a in calls)
  corners = [a for kind, a in calls if kind == 'corner']
  assert len(corners) == 4 * sum(sides)
  for points, color in corners:
    assert len(points) == 20
    ox, oy = points[0]
    assert (ox, oy) in ((0, 0), (0, 480), (1920, 0), (1920, 480))
    assert color.a in (alpha, int(alpha * .3))
    assert all(0 <= y <= 480 and (0 <= x <= 72 or 1848 <= x <= 1920) for x, y in points)
    # Every triangle has consistent winding and nonzero area; the square
    # physical corner is filled, with no self-overlap within either layer.
    for (ax, ay), (bx, by) in zip(points[1:-1], points[2:]):
      assert (ax - ox) * (by - oy) - (ay - oy) * (bx - ox) < 0
    cx, cy = (72 if ox == 0 else 1848), (72 if oy == 0 else 408)
    radius = 48 if color.a == alpha else 24
    for x, y in points[2:-1]:
      assert (x - cx) ** 2 + (y - cy) ** 2 == pytest.approx(radius ** 2, abs=.02)
  for kind, a in calls:
    if kind == 'gradient' and a[3] in (24, 48):
      opacity = alpha if a[3] == 24 else int(alpha * .3)
      # Both top and bottom must fade in the correct physical direction,
      # including the halo; the off phase has no warning geometry.
      assert (a[4].a, a[5].a) == ((opacity, 0) if a[0] < 640 else (0, opacity))
  for kind, rect in calls:
    if kind in ('edge', 'gradient'):
      x, y, w, h = rect[:4]
      assert 0 <= y < y + h <= 480
      assert 0 <= x < x + w <= 640 or 1280 <= x < x + w <= 1920
  assert calls[-1][0] == 'vehicle'
