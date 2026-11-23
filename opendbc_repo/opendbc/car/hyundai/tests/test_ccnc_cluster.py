import copy
from types import SimpleNamespace

import pytest

from opendbc.can import CANDefine, CANPacker
from opendbc.can.parser import get_raw_value
from opendbc.car import structs
from opendbc.car.hyundai import hyundaicanfd
from opendbc.car.hyundai.hyundaicanfd import _select_cluster_background
from opendbc.car.hyundai.pv5_ccnc_display import Pv5CcncDisplay, plan_signal_state
from opendbc.car.hyundai.values import CAR, HyundaiFlags


@pytest.mark.parametrize(
  ("cruise_enabled", "lat_active", "paddle_pressed", "paddle_mode", "expected"),
  (
    (True, True, True, 0, 1),
    (False, True, True, 0, 3),
    (False, False, True, 0, 7),
    (True, True, True, 1, 6),
    (True, True, False, 1, 1),
  ),
)
def test_paddle_background_requires_enabled_paddle_mode(
  cruise_enabled, lat_active, paddle_pressed, paddle_mode, expected,
):
  assert _select_cluster_background(cruise_enabled, lat_active, paddle_pressed, paddle_mode) == expected


@pytest.mark.parametrize('cruise_enabled', (False, True))
@pytest.mark.parametrize('lat_active', (False, True))
def test_model_turn_background_requires_active_steering(cruise_enabled, lat_active):
  normal = 1 if cruise_enabled else 7
  assert _select_cluster_background(cruise_enabled, lat_active, False, 0, turning=True) == (6 if lat_active else normal)


@pytest.mark.parametrize('enabled,lat_active', ((False, False), (False, True), (True, False), (True, True)))
@pytest.mark.parametrize('turning', (False, True))
def test_pv5_turn_is_gray_bsd_is_steady_red_and_paddle_takes_priority(enabled, lat_active, turning):
  palette = CANDefine('hyundai_canfd_generated').dv['ADRV_0x161']['BACKGROUND']
  assert palette[3] == 'ORANGE'
  assert palette[6] == 'FLASHING_RED'
  assert palette[7] == 'GRAY'
  normal = 1 if enabled else 3 if lat_active else 7
  args = (enabled, lat_active, False, 0, turning)
  assert _select_cluster_background(*args, turn_background=7) == (7 if lat_active and turning else normal)
  assert _select_cluster_background(*args, turn_background=7, bsd_warning=True) == 5
  assert _select_cluster_background(enabled, lat_active, True, 1, turning, turn_background=7) == 6
  assert _select_cluster_background(enabled, lat_active, True, 1, turning, turn_background=7, bsd_warning=True) == 6


@pytest.mark.parametrize('signals', ((False, False), (True, False), (False, True), (True, True)))
@pytest.mark.parametrize('warnings', ((False, False), (True, False), (False, True), (True, True)))
@pytest.mark.parametrize('valid,timeout', ((True, False), (False, False), (True, True)))
def test_bsd_background_requires_valid_same_side_request(signals, warnings, valid, timeout):
  out = structs.CarState(canValid=valid, canTimeout=timeout,
                         leftBlinker=signals[0], rightBlinker=signals[1],
                         leftBlindspot=warnings[0], rightBlindspot=warnings[1])
  expected = valid and not timeout and any(s and w for s, w in zip(signals, warnings, strict=True))
  assert hyundaicanfd._cluster_bsd_warning(out) == expected


@pytest.mark.parametrize('enabled,lat_active', ((False, False), (False, True), (True, True)))
@pytest.mark.parametrize('pv5', (False, True))
@pytest.mark.parametrize('desire', range(5))
def test_bsd_background_can_frames_preserve_payload_and_restore_on_event_end(monkeypatch, enabled, lat_active, pv5, desire):
  from openpilot.cereal import log
  monkeypatch.setattr(hyundaicanfd, 'Params', lambda: SimpleNamespace(get_int=lambda key: 0, get=lambda key: '0'))
  packer = CANPacker('hyundai_canfd_generated')
  baseline_packer = CANPacker('hyundai_canfd_generated')
  definition = packer.dbc.name_to_msg['ADRV_0x161']
  stock = {key: 0 for key in definition.sigs}
  stock.update(COUNTER=17, ALERTS_4=7, SOUNDS_4=2)
  original = stock.copy()
  out = structs.CarState(canValid=True, leftBlinker=True, leftBlindspot=True)
  model = log.ModelDataV2.new_message()
  model.meta.desire = desire
  state = SimpleNamespace(
    adrv_0x161=stock, out=out, modelV2=model, lfahda_cluster=None,
    cruise_buttons_msg=None, adrv_0x200=None, adrv_0x1ea=None, ccnc_0x162=None,
    is_metric=True, paddle_button_prev=0, softHoldActive=0, trailer_connected=False,
  )
  control = structs.CarControl(latActive=lat_active, enabled=enabled)
  cp = SimpleNamespace(flags=HyundaiFlags.CAMERA_SCC, carFingerprint=CAR.KIA_PV5 if pv5 else 'OTHER')

  def messages(frame, selected_packer=packer):
    result = hyundaicanfd.create_ccnc_messages(
      cp, selected_packer, SimpleNamespace(ECAN=0, CAM=2), frame, control, state, control.hudControl,
      0, False, False, 0, False, 0, 0)
    assert len(result) == 1
    address, data, bus = result[0]
    assert (address, bus) == (0x161, 0)
    decoded = {key: get_raw_value(data, sig) for key, sig in definition.sigs.items()}
    assert decoded['CHECKSUM'] == hyundaicanfd.hkg_can_fd_checksum(address, None, bytearray(data))
    return decoded

  normal = 1 if enabled else 3 if lat_active else 7
  fallback = (7 if pv5 else 6) if lat_active and desire in (1, 2) else normal
  selector = hyundaicanfd._select_cluster_background
  previous_counter = None
  # Compare the selector at the identical frame/state, allowing existing icons
  # to respond normally when the driver request or BSD state changes.
  for frame, attr, value in ((5, 'leftBlindspot', True), (10, 'leftBlindspot', True),
                             (15, 'leftBlindspot', False), (20, 'leftBlindspot', True),
                             (25, 'canValid', False), (30, 'canValid', True),
                             (35, 'leftBlinker', False)):
    setattr(out, attr, value)
    actual = messages(frame)
    with monkeypatch.context() as patch:
      patch.setattr(hyundaicanfd, '_select_cluster_background',
                    lambda *args, **kwargs: selector(*args, **dict(kwargs, bsd_warning=False, turn_background=6)))
      baseline = messages(frame, baseline_packer)
    warning = pv5 and out.canValid and out.leftBlinker and out.leftBlindspot
    assert actual['BACKGROUND'] == (5 if warning else fallback)
    assert actual['LANE_HIGHLIGHT'] == (5 if warning and (frame // 20) % 2 == 0 else baseline['LANE_HIGHLIGHT'])
    assert actual['BCA_LEFT'] == (2 if warning else baseline['BCA_LEFT'])
    assert actual['BCA_RIGHT'] == baseline['BCA_RIGHT']
    assert {k: v for k, v in actual.items() if k not in ('BACKGROUND', 'LANE_HIGHLIGHT', 'BCA_LEFT', 'BCA_RIGHT', 'CHECKSUM')} == {
      k: v for k, v in baseline.items() if k not in ('BACKGROUND', 'LANE_HIGHLIGHT', 'BCA_LEFT', 'BCA_RIGHT', 'CHECKSUM')}
    if previous_counter is not None:
      assert actual['COUNTER'] == (previous_counter + 1) % 256
    previous_counter = actual['COUNTER']
  assert stock == original


@pytest.mark.parametrize('fingerprint,not_car,preserve_stock', (
  (CAR.KIA_PV5, False, True),
  (CAR.KIA_PV5, True, False),
  (CAR.KIA_EV9, False, False),
))
@pytest.mark.parametrize('lat_active', (False, True))
@pytest.mark.parametrize('trailer', (False, True))
def test_ccnc_preserves_original_lane_fields_only_for_real_pv5(monkeypatch, fingerprint, not_car, preserve_stock, lat_active, trailer):
  from openpilot.cereal import log
  monkeypatch.setattr(hyundaicanfd, 'Params', lambda: SimpleNamespace(get_int=lambda key: 0, get=lambda key: '0'))
  packer = CANPacker('hyundai_canfd_generated')
  definition = packer.dbc.name_to_msg['ADRV_0x161']
  original_lines = dict(LANELINE_LEFT=8, LANELINE_RIGHT=2, LANELINE_LEFT_POSITION=12,
                        LANELINE_RIGHT_POSITION=14, LANELINE_CURVATURE=7, LANELINE_CURVATURE_DIRECTION=1)
  stock = dict.fromkeys(definition.sigs, 0)
  stock.update(original_lines, COUNTER=17)
  original = stock.copy()
  state = SimpleNamespace(
    adrv_0x161=stock, lfahda_cluster=None, cruise_buttons_msg=None,
    adrv_0x200=None, adrv_0x1ea=None, ccnc_0x162=None, is_metric=True,
    paddle_button_prev=0, softHoldActive=0, trailer_connected=trailer,
    out=structs.CarState(canValid=True, steeringAngleDeg=9., leftBlinker=True, leftBlindspot=True),
    modelV2=log.ModelDataV2.new_message(),
  )
  cp = SimpleNamespace(flags=HyundaiFlags.CAMERA_SCC, carFingerprint=fingerprint, notCar=not_car)
  control = structs.CarControl(latActive=lat_active, enabled=lat_active)
  control.hudControl.leftLaneVisible = control.hudControl.rightLaneVisible = True
  control.hudControl.leftLaneDepart = True
  (address, payload, bus), = hyundaicanfd.create_ccnc_messages(
    cp, packer, SimpleNamespace(ECAN=0, CAM=2), 5, control, state, control.hudControl,
    0, False, False, 0, False, 0, 0)
  assert (address, bus) == (0x161, 0)
  values = {key: get_raw_value(payload, sig) for key, sig in definition.sigs.items()}
  expected = original_lines if preserve_stock else dict(
    original_lines, LANELINE_LEFT=2 if trailer else 4,
    LANELINE_CURVATURE=3 if lat_active else 0, LANELINE_CURVATURE_DIRECTION=0)
  assert {key: values[key] for key in original_lines} == expected
  if fingerprint == CAR.KIA_PV5:
    assert values['BACKGROUND'] == values['LANE_HIGHLIGHT'] == 5
    assert values['BCA_LEFT'] == 2
  assert values['COUNTER'] == 18
  assert values['CHECKSUM'] == hyundaicanfd.hkg_can_fd_checksum(address, None, bytearray(payload))
  assert stock == original


@pytest.mark.parametrize('left,right', ((True, False), (False, True), (True, True)))
def test_pv5_bsd_guide_is_independent_of_lane_visibility_with_exact_2_5hz(left, right):
  out = structs.CarState(canValid=True, leftBlinker=left, rightBlinker=right,
                        leftBlindspot=left, rightBlindspot=right)
  highlights = []
  for frame in range(0, 200, 5):
    values = {"LANE_HIGHLIGHT": 0, "BCA_LEFT": 0, "BCA_RIGHT": 0, "ALERTS_1": 123, "SOUNDS_1": 3}
    hyundaicanfd._apply_pv5_bsd_cluster_guide(values, out, frame)
    highlights.append(values['LANE_HIGHLIGHT'])
    assert values['BCA_LEFT'] == (2 if left else 0)
    assert values['BCA_RIGHT'] == (2 if right else 0)
    assert (values['ALERTS_1'], values['SOUNDS_1']) == (123, 3)
  assert highlights == [5] * 4 + [0] * 4 + ([5] * 4 + [0] * 4) * 4
  out.leftBlinker = out.rightBlinker = False
  values = {"LANE_HIGHLIGHT": 0, "BCA_LEFT": 0, "BCA_RIGHT": 0}
  hyundaicanfd._apply_pv5_bsd_cluster_guide(values, out, 200)
  assert values == {"LANE_HIGHLIGHT": 0, "BCA_LEFT": 0, "BCA_RIGHT": 0}


def test_pv5_bsd_guide_does_not_erase_oem_warning_on_off_phase_or_bad_can():
  out = structs.CarState(canValid=True, leftBlinker=True, leftBlindspot=True)
  original = {"LANE_HIGHLIGHT": 5, "BCA_LEFT": 2, "BCA_RIGHT": 2, "ALERTS_1": 123}
  values = original.copy()
  hyundaicanfd._apply_pv5_bsd_cluster_guide(values, out, 20)
  assert values == original
  out.canValid = False
  values = {"LANE_HIGHLIGHT": 0, "BCA_LEFT": 0, "BCA_RIGHT": 0}
  hyundaicanfd._apply_pv5_bsd_cluster_guide(values, out, 0)
  assert values == {"LANE_HIGHLIGHT": 0, "BCA_LEFT": 0, "BCA_RIGHT": 0}


@pytest.mark.parametrize('enabled,lat_active', ((False, False), (True, True)))
@pytest.mark.parametrize('desire', (0, 1))
@pytest.mark.parametrize('paddle', (False, True))
@pytest.mark.parametrize('pv5', (False, True))
def test_bsd_background_stays_steady_without_extra_can_frames_or_paddle_changes(monkeypatch, enabled, lat_active, desire, paddle, pv5):
  from openpilot.cereal import log
  monkeypatch.setattr(hyundaicanfd, 'Params', lambda: SimpleNamespace(get_int=lambda key: 0, get=lambda key: '0'))
  packer = CANPacker('hyundai_canfd_generated')
  baseline_packer = CANPacker('hyundai_canfd_generated')
  definition = packer.dbc.name_to_msg['ADRV_0x161']
  stock = {key: 0 for key in definition.sigs}
  stock.update(COUNTER=17, ALERTS_4=7, SOUNDS_4=2)
  original = stock.copy()
  model = log.ModelDataV2.new_message()
  model.meta.desire = desire
  out = structs.CarState(canValid=True, leftBlinker=True, leftBlindspot=True)
  state = SimpleNamespace(
    adrv_0x161=stock, out=out, modelV2=model, lfahda_cluster=None,
    cruise_buttons_msg=None, adrv_0x200=None, adrv_0x1ea=None, ccnc_0x162=None,
    is_metric=True, paddle_button_prev=int(paddle), softHoldActive=0, trailer_connected=False,
  )
  control = structs.CarControl(latActive=lat_active, enabled=enabled)
  cp = SimpleNamespace(flags=HyundaiFlags.CAMERA_SCC, carFingerprint=CAR.KIA_PV5 if pv5 else 'OTHER')
  selector = hyundaicanfd._select_cluster_background
  backgrounds = []
  counters = []

  def messages(frame, selected_packer):
    return hyundaicanfd.create_ccnc_messages(
      cp, selected_packer, SimpleNamespace(ECAN=0, CAM=2), frame, control, state, control.hudControl,
      0, False, False, 0, False, 0, 1)

  # Exercise every 100 Hz controller tick for two seconds, including ticks
  # without a 0x161 transmission. No extra frames or high-rate sender.
  for frame in range(200):
    actual = messages(frame, packer)
    with monkeypatch.context() as patch:
      patch.setattr(hyundaicanfd, '_select_cluster_background',
                    lambda *args, **kwargs: selector(*args, **dict(kwargs, bsd_warning=False)))
      baseline = messages(frame, baseline_packer)
    assert len(actual) == len(baseline) == (1 if frame % 5 == 0 else 0)
    if not actual:
      continue
    assert (actual[0][0], actual[0][2]) == (baseline[0][0], baseline[0][2]) == (0x161, 0)
    values = {key: get_raw_value(actual[0][1], sig) for key, sig in definition.sigs.items()}
    old = {key: get_raw_value(baseline[0][1], sig) for key, sig in definition.sigs.items()}
    assert {k: v for k, v in values.items() if k not in ('BACKGROUND', 'LANE_HIGHLIGHT', 'BCA_LEFT', 'BCA_RIGHT', 'CHECKSUM')} == {
      k: v for k, v in old.items() if k not in ('BACKGROUND', 'LANE_HIGHLIGHT', 'BCA_LEFT', 'BCA_RIGHT', 'CHECKSUM')}
    assert values['CHECKSUM'] == hyundaicanfd.hkg_can_fd_checksum(0x161, None, bytearray(actual[0][1]))
    warning = pv5 and out.leftBlinker and out.leftBlindspot
    assert values['LANE_HIGHLIGHT'] == (5 if warning and (frame // 20) % 2 == 0 else old['LANE_HIGHLIGHT'])
    assert values['BCA_LEFT'] == (2 if warning else old['BCA_LEFT'])
    assert values['BCA_RIGHT'] == old['BCA_RIGHT']
    backgrounds.append(values['BACKGROUND'])
    counters.append(values['COUNTER'])

  normal = 1 if enabled else 3 if lat_active else 7
  fallback = (7 if pv5 else 6) if lat_active and desire == 1 else normal
  expected = [6 if paddle else 5 if pv5 else fallback] * 40
  assert backgrounds == expected
  assert all(b == (a + 1) % 256 for a, b in zip(counters[:-1], counters[1:], strict=True))
  assert stock == original


@pytest.mark.parametrize('desire', range(5))
@pytest.mark.parametrize('lat_active', (False, True))
@pytest.mark.parametrize('pv5', (False, True))
def test_turn_changes_only_background_in_existing_cluster_frame(monkeypatch, desire, lat_active, pv5):
  from openpilot.cereal import log
  monkeypatch.setattr(hyundaicanfd, 'Params', lambda: SimpleNamespace(get_int=lambda key: 0, get=lambda key: '0'))
  packer = CANPacker('hyundai_canfd_generated')
  definition = packer.dbc.name_to_msg['ADRV_0x161']
  stock = {key: 0 for key in definition.sigs}
  stock['COUNTER'] = 17
  original = stock.copy()
  model = log.ModelDataV2.new_message()
  model.meta.desire = desire
  state = SimpleNamespace(
    adrv_0x161=stock, out=structs.CarState(), modelV2=model, lfahda_cluster=None,
    cruise_buttons_msg=None, adrv_0x200=None, adrv_0x1ea=None, ccnc_0x162=None,
    is_metric=True, paddle_button_prev=0, softHoldActive=0, trailer_connected=False,
  )
  control = structs.CarControl(latActive=lat_active, enabled=True)

  def messages():
    return hyundaicanfd.create_ccnc_messages(
      SimpleNamespace(flags=HyundaiFlags.CAMERA_SCC, carFingerprint=CAR.KIA_PV5 if pv5 else 'OTHER'), CANPacker('hyundai_canfd_generated'),
      SimpleNamespace(ECAN=0, CAM=2), 5, control, state, control.hudControl, 0, False, False, 0, False, 0, 0)
  actual = messages()
  monkeypatch.setattr(hyundaicanfd, '_select_cluster_background',
                      lambda enabled, lat, paddle, mode, **kwargs: 6 if mode > 0 and paddle else 1 if enabled else 3 if lat else 7)
  baseline = messages()
  assert len(actual) == len(baseline) == 1
  assert (actual[0][0], actual[0][2]) == (baseline[0][0], baseline[0][2]) == (0x161, 0)

  def decode(message):
    return {key: get_raw_value(message[1], sig) for key, sig in definition.sigs.items()}
  values, old = decode(actual[0]), decode(baseline[0])
  assert values['BACKGROUND'] == ((7 if pv5 else 6) if lat_active and desire in (1, 2) else 1)
  assert {k: v for k, v in values.items() if k not in ('BACKGROUND', 'CHECKSUM')} == {
    k: v for k, v in old.items() if k not in ('BACKGROUND', 'CHECKSUM')}
  assert values['CHECKSUM'] == hyundaicanfd.hkg_can_fd_checksum(0x161, None, bytearray(actual[0][1]))
  assert stock == original


@pytest.mark.parametrize("distance", (0.0, 1.6, 14.0, 14.1, 20.0, 25.5))
@pytest.mark.parametrize(("message", "detect", "expected_corner", "expected_front"), [
  *(('ADRV_0x1ea', detect, expected, None) for detect, expected in (
    (0, 0), (1, 1), (2, 2), (3, 3), (4, 1), (5, 1), (6, 1), (7, 1),
  )),
  *(('CCNC_0x162', detect, 3, expected) for detect, expected in (
    (1, 3), (2, 4), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7),
    (8, 8), (9, 9), (10, 10), (11, 11), (12, 12), (13, 13), (14, 14),
  )),
  # EV5 supplies nonzero corner geometry while 0x162 DETECT stays hidden.
  ('CCNC_0x162', 0, 3, 0),
])
def test_cluster_objects_restore_corner_state_without_blinking_or_distance_clamp(monkeypatch, message, distance, detect,
                                                                               expected_corner, expected_front):
  monkeypatch.setattr(hyundaicanfd, "Params", lambda: SimpleNamespace(get_int=lambda key: 0))
  packer = CANPacker("hyundai_canfd_generated")
  display_types = CANDefine("hyundai_canfd_generated").dv[message]
  definition = packer.dbc.name_to_msg[message]
  source = {key: 0 for key in definition.sigs}
  for side in ("LF", "RF", "LR", "RR"):
    source[f"{side}_DETECT"] = detect
    source[f"{side}_DETECT_DISTANCE"] = distance
    source[f"{side}_DETECT_LATERAL"] = 2.9
  if message == "CCNC_0x162":
    source.update(FF_DETECT=detect, FF_DISTANCE=81.2, FF_LATERAL=1.3,
                  FF_DETECT_ALT=2, FF_DISTANCE_ALT=42.1, FF_LATERAL_ALT=0.7)
  original = copy.deepcopy(source)
  state = SimpleNamespace(
    out=SimpleNamespace(brakeHoldActive=False, parkingBrake=False, steeringAngleDeg=6.0,
                        leftBlinker=False, rightBlinker=False),
    modelV2=None, lfahda_cluster=None, cruise_buttons_msg=None, adrv_0x161=None, adrv_0x200=None,
    adrv_0x1ea=source if message == "ADRV_0x1ea" else None,
    ccnc_0x162=source if message == "CCNC_0x162" else None,
    radarState=SimpleNamespace(leadOne=SimpleNamespace(status=True, dRel=81.2, yRel=-1.3, vRel=-5.0)),
  )
  cp = SimpleNamespace(flags=HyundaiFlags.CAMERA_SCC.value)
  can = SimpleNamespace(ECAN=0, CAM=2)
  control = SimpleNamespace(latActive=True, enabled=True)
  # FF uses radarState directly, independently of the older HUD distance.
  hud = SimpleNamespace(leadDistance=123.4, leadRadar=1, leadRelSpeed=-5.0)

  for frame in (0, 5, 65, 70, 100, 135, 200):
    messages = hyundaicanfd.create_ccnc_messages(cp, packer, can, frame, control, state, hud,
                                                0, False, False, 0, False, 0, 0)
    assert len(messages) == 1
    address, data, bus = messages[0]
    assert (address, bus) == (definition.address, 0)
    decoded = {key: get_raw_value(data, sig) * sig.factor + sig.offset for key, sig in definition.sigs.items()}
    assert decoded["CHECKSUM"] == hyundaicanfd.hkg_can_fd_checksum(address, None, bytearray(data))
    for side in ("LF", "RF", "LR", "RR"):
      assert decoded[f"{side}_DETECT_DISTANCE"] == pytest.approx(distance)
      assert decoded[f"{side}_DETECT_LATERAL"] == pytest.approx(2.9)
      assert decoded[f"{side}_DETECT"] == (expected_corner if distance != 0 else detect)
      if message == "CCNC_0x162" and distance != 0:
        assert display_types[f"{side}_DETECT"][decoded[f"{side}_DETECT"]] == "GRAY_CAR"
    if message == "CCNC_0x162":
      assert decoded["FF_DETECT"] == (expected_front or 4)
      for key in ("FF_DISTANCE", "FF_LATERAL", "FF_DETECT_ALT", "FF_DISTANCE_ALT", "FF_LATERAL_ALT"):
        assert decoded[key] == pytest.approx(original[key])
    else:
      assert decoded["LANELINE_CURVATURE"] == 2
    assert source == original


@pytest.mark.parametrize("traffic,observation,stop,suspended,fcw,expected", (
  (0, 2, False, False, False, 0),
  (1, 0, True, False, False, 1),
  (1, 1, True, False, False, 3),
  (1, 2, True, False, False, 0),
  (1, 1, False, False, False, 0),
  (1, 1, True, True, False, 0),
  (1, 1, True, False, True, 0),
  (2, 0, False, False, False, 0),
  (2, 3, False, False, False, 0),
  (2, 2, False, False, False, 2),
))
def test_pv5_signal_uses_stop_and_green_observation(traffic, observation, stop, suspended, fcw, expected):
  plan = SimpleNamespace(trafficState=traffic, signalObservationColor=observation, shouldStop=stop,
                         signalDetectionSuspended=suspended, fcw=fcw)
  assert plan_signal_state(plan) == expected


@pytest.mark.parametrize("mode", (0, 1, 2))
def test_pv5_signal_green_requires_continuous_confirmed_red(mode):
  display = Pv5CcncDisplay()
  display.configure(SimpleNamespace(get_int=lambda key: mode if key == "Pv5CcncTrafficBackground" else 0))
  hud = structs.CarControl.new_message().hudControl
  now = 1_000_000_000

  def step(state, allowed=True, stopped=True, elapsed_ms=50):
    nonlocal now
    now += elapsed_ms * 1_000_000
    hud.pv5CcncTrafficState = state
    return display.traffic_background(hud, now, allowed, stopped)

  assert not any(step(2) for _ in range(4))
  assert not any(step(3) for _ in range(6))
  assert step(3) == (5 if mode else 0)
  assert step(2) == (8 if mode == 2 else 0)
  assert [step(2) for _ in range(40)] == ([8] * 39 + [0] if mode == 2 else [0] * 40)
  assert step(2) == 0
  for interrupt in (dict(allowed=False), dict(stopped=False), dict(elapsed_ms=250), dict(elapsed_ms=-50)):
    for _ in range(8):
      step(3)
    assert step(3, **interrupt) == 0
    assert step(2) == 0
  for _ in range(8):
    step(3)
  assert step(0) == 0
  assert step(2) == 0
  assert not any(step(3) for _ in range(6))
  assert step(2) == 0  # At the boundary, red has not been displayed yet.
  for _ in range(8):
    red = step(1)
  assert red == (5 if mode else 0)
  assert step(2) == 0  # A plan-only red does not arm green.


@pytest.mark.parametrize("bars,expected", ((0, 4), (1, 1), (2, 2), (3, 3), (4, 4)))
def test_pv5_hdp_shows_selected_gap_in_existing_frame(monkeypatch, bars, expected):
  monkeypatch.setattr(hyundaicanfd, "Params",
                      lambda: SimpleNamespace(get_int=lambda key: 0, get=lambda key: "2"))
  display = Pv5CcncDisplay()
  packer = CANPacker("hyundai_canfd_generated")
  definition = packer.dbc.name_to_msg["ADRV_0x161"]
  stock = dict.fromkeys(definition.sigs, 0)
  original = stock.copy()
  state = SimpleNamespace(
    adrv_0x161=stock, lfahda_cluster=None, cruise_buttons_msg=None, adrv_0x200=None,
    adrv_0x1ea=None, ccnc_0x162=None, radarState=SimpleNamespace(
      leadOne=SimpleNamespace(status=True, dRel=30.0, yRel=0.0, vRel=0.0)),
    out=structs.CarState(canValid=True, gearShifter="drive", standstill=True),
    modelV2=None, is_metric=True, paddle_button_prev=0, softHoldActive=0, trailer_connected=False,
  )
  cp = SimpleNamespace(flags=HyundaiFlags.CAMERA_SCC, carFingerprint=CAR.KIA_PV5)
  control = structs.CarControl(enabled=True, latActive=True, longActive=True)
  hud = control.hudControl
  hud.leadDistanceBars = bars
  hud.pv5CcncTargetDistance = 16.0
  hud.pv5CcncLaneHighlight = 3
  hud.leftLaneVisible = True

  def frame(index, instance, expected_stamp_age_ms=100, paddle_mode=0):
    now = 1_000_000_000 + index * 50_000_000
    hud.pv5CcncPlanMonoTime = now - expected_stamp_age_ms * 1_000_000
    hud.pv5CcncLaneMonoTime = hud.pv5CcncPlanMonoTime
    (address, data, bus), = hyundaicanfd.create_ccnc_messages(
      cp, packer, SimpleNamespace(ECAN=0, CAM=2), index * 5, control, state, hud,
      0, False, False, 0, False, 0, paddle_mode, pv5_display=instance, now_ns=now,
    )
    assert (address, bus) == (0x161, 0)
    result = {name: get_raw_value(data, sig) * sig.factor + sig.offset for name, sig in definition.sigs.items()}
    assert result["CHECKSUM"] == hyundaicanfd.hkg_can_fd_checksum(address, None, bytearray(data))
    return result

  default = frame(0, display)
  old = frame(1, None)
  assert (old["DISTANCE"], default["DISTANCE"]) == (4, expected)
  assert default["DISTANCE_SPACING"] == (5 if expected == 4 else 3)
  assert default["TARGET_DISTANCE"] == 30.0
  changed = {"DISTANCE", "DISTANCE_SPACING", "CHECKSUM", "COUNTER"}
  assert all(default[key] == old[key] for key in default if key not in changed)

  display.configure(SimpleNamespace(get_int=lambda key: 1))
  fresh = frame(2, display)
  stale = frame(3, display, expected_stamp_age_ms=201)
  assert (fresh["TARGET_DISTANCE"], fresh["LANE_HIGHLIGHT"]) == (16.0, 3)
  assert (stale["TARGET_DISTANCE"], stale["LANE_HIGHLIGHT"]) == (30.0, default["LANE_HIGHLIGHT"])
  changed.update(("TARGET_DISTANCE", "LANE_HIGHLIGHT"))
  assert all(fresh[key] == old[key] for key in fresh if key not in changed)

  hud.pv5CcncTrafficState = 1
  for index in range(4, 11):
    red = frame(index, display)
  assert red["BACKGROUND"] == 5
  state.out.leftBlinker = state.out.leftBlindspot = True
  state.paddle_button_prev = 1
  assert frame(11, display, paddle_mode=1)["BACKGROUND"] == 6
  assert frame(12, display)["BCA_LEFT"] == 2
  state.out.leftBlinker = state.out.leftBlindspot = False
  hud.visualAlert = "fcw"
  assert frame(13, display)["BACKGROUND"] == 1
  assert stock == original
