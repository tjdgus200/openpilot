"""PV5 opt-in regressions for upstream 44306cae, without changing other cars."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.carrot.radar_motion.controller import DPathRadarController
from openpilot.selfdrive.carrot.radar_motion.controller import pv5_vision_transition_enabled
from openpilot.selfdrive.carrot.tests.test_radar_motion_predictor import Point, model_with_lead, moving_range_model, seed_moving_range_front

from openpilot.selfdrive.carrot.tests.test_stationary_front_evidence import approach_frame, seed_stationary


@pytest.mark.parametrize("brand,fingerprint,enabled", (
  ("hyundai", "KIA_PV5", True),
  ("hyundai", "KIA_EV9", False),
  ("hyundai", "KIA_CARNIVAL_4TH_GEN", False),
  ("volkswagen", "VOLKSWAGEN_ID4", False),
  ("toyota", "TOYOTA_RAV4", False),
  ("", "", False),
  ("hyundai", "", False),
  ("volkswagen", "KIA_PV5", False),
))
def test_live_radar_constructor_applies_only_to_pv5(brand, fingerprint, enabled):
  # Execute the real constructor without opening messaging sockets or Params.
  import ast
  from pathlib import Path
  from openpilot.cereal import log
  from openpilot.selfdrive.carrot.radar import effective_radar_track_mode
  from openpilot.selfdrive.carrot.radar_motion.timing import front_radar_distance_delay_s

  path = Path(__file__).parents[1] / "radar/radard_dpath.py"
  tree = ast.parse(path.read_text(encoding="utf-8"))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DPathRadarD")
  init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
  sensitivity = next(n for n in tree.body if isinstance(n, ast.Assign) and any(
    isinstance(t, ast.Name) and t.id == "PRODUCTION_CUT_IN_SENSITIVITY" for t in n.targets
  ))
  ns = {"car": SimpleNamespace(CarParams=object), "log": log,
            "Params": lambda: SimpleNamespace(get_int=lambda key: 2 if key == "EnableRadarTracks" else 0),
            "DPathRadarController": DPathRadarController, "effective_radar_track_mode": effective_radar_track_mode,
            "corner_radar_enabled": lambda *args: False, "front_radar_distance_delay_s": front_radar_distance_delay_s,
            "pv5_vision_transition_enabled": pv5_vision_transition_enabled}
  exec(compile(ast.Module(body=[sensitivity, init], type_ignores=[]), str(path), "exec"), ns)
  instance = SimpleNamespace()
  cp = SimpleNamespace(brand=brand, carFingerprint=fingerprint, radarUnavailable=False, radarDelay=0., flags=0)
  ns["__init__"](instance, cp)
  assert instance.controller.confirm_vision_transition is enabled
  assert instance.controller.enable_radar_tracks == (2 if brand == "hyundai" else 1)


@pytest.mark.parametrize("mode", (-2, -1, 0, 1, 2, 3))
def test_default_selection_keeps_immediate_visual_fallback(mode):
  controller = DPathRadarController(enable_radar_tracks=mode, cut_in_sensitivity=0)
  point = Point(281, 50., 0.1, trackState=2)
  controller.update(0., 25., (point,), model_with_lead(50., 0.1, 25.))
  out = controller.update(0.05, 25., (point,), model_with_lead(10., 0.1, 10.))
  assert not controller.confirm_vision_transition
  if mode > 0 or mode == -2:
    assert out.lead_one["radarTrackId"] == -1
    assert out.lead_one["dRel"] == pytest.approx(10.)


@pytest.mark.parametrize("mode", (1, 2, 3))
@pytest.mark.parametrize("distance,velocity", ((4., 0.), (8., 5.), (30., 10.)))
def test_no_previous_healthy_radar_never_waits(mode, distance, velocity):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode)
  out = controller.update(0., 25., (), model_with_lead(distance, 0., velocity))
  assert out.lead_one["radarTrackId"] == -1
  assert out.lead_one["dRel"] == pytest.approx(distance)


@pytest.mark.parametrize("brand,fingerprint,mixed", (
  ("hyundai", "KIA_PV5", False), ("hyundai", "KIA_EV9", False), ("", "", False),
  ("hyundai", "KIA_PV5", True),
))
def test_replay_uses_recorded_vehicle_scope(brand, fingerprint, mixed):
  from openpilot.selfdrive.carrot.radar.tools.radar_validation_replay import (
    ModelLead, _production_controller_outputs,
  )
  from openpilot.selfdrive.carrot.tests.test_radar_lead_simulator import frame, point

  frames = [replace(frame((replace(point(281, 50., 0.1), v_lead=25., radar_track_state=2),), time_s=t),
                    v_ego=25., car_brand=brand, car_fingerprint=fingerprint,
                    model_leads=(ModelLead(.9, d+1.52, -.1, v, 0., 2., .6, 1.5),))
            for t, d, v in ((0., 50., 25.), (.05, 30., 10.), (.10, 29.25, 10.),
                            (.15, 28.5, 10.), (.20, 27.75, 10.), (.25, 27., 10.))]
  if mixed:
    frames[-1] = replace(frames[-1], car_fingerprint="")
  outputs = _production_controller_outputs(frames, motion_sensor="front", enable_radar_tracks=2, cut_in_sensitivity=0)
  assert outputs[0].lead_one["radarTrackId"] == 281
  assert outputs[1].lead_one["radarTrackId"] == (281 if fingerprint == "KIA_PV5" and not mixed else -1)
  assert outputs[-1].lead_one["radarTrackId"] == -1


def test_scope_matches_the_actual_pv5_platform_identifier():
  from opendbc.car.hyundai.values import CAR
  assert pv5_vision_transition_enabled("hyundai", CAR.KIA_PV5)
  assert not pv5_vision_transition_enabled("hyundai", CAR.KIA_EV9)


@pytest.mark.parametrize("fingerprint", ("KIA_PV5", "KIA_EV9"))
def test_log_loader_keeps_late_vehicle_metadata_for_earlier_frames(tmp_path, fingerprint):
  from openpilot.cereal import log
  from openpilot.selfdrive.carrot.radar.tools.radar_validation_replay import load_frames

  events = []
  for service, stamp in (("liveTracks", 1_000_000_000), ("modelV2", 1_010_000_000), ("carParams", 1_020_000_000)):
    event = log.Event.new_message(logMonoTime=stamp)
    event.init(service)
    events.append(event)
  events[0].liveTracks.points = [{"trackId": 281, "dRel": 50., "yRel": .1,
                                 "vLead": 25., "measured": True, "radarSource": "frontRadar", "trackState": 2}]
  events[1].modelV2.timestampEof = 1_005_000_000
  events[1].modelV2.position.x = [0., 100.]
  events[1].modelV2.position.y = [0., 0.]
  events[1].modelV2.velocity.x = [25.]
  events[2].carParams.brand = "hyundai"
  events[2].carParams.carFingerprint = fingerprint
  path = tmp_path / "synthetic.rlog"
  path.write_bytes(b"".join(event.to_bytes() for event in events))
  frames = load_frames(path)
  assert len(frames) == 1
  assert frames[0].car_brand == "hyundai"
  assert frames[0].car_fingerprint == fingerprint


@pytest.mark.parametrize("confirm", (False, True))
@pytest.mark.parametrize("mode", (1, 2, 3))
@pytest.mark.parametrize("distance,speed", ((4., 0.), (10., 5.), (30., 10.)))
def test_transition_outputs_feed_actual_mpc_preparation(confirm, mode, distance, speed):
  """Execute production process_lead/update; solver records, it does not solve."""
  import numpy as np
  from openpilot.cereal import log
  from openpilot.selfdrive.carrot.tests.test_cutout_mpc_integration import run_update

  controller = DPathRadarController(confirm_vision_transition=confirm, enable_radar_tracks=mode, cut_in_sensitivity=0)
  point = seed_vision_transition_front(controller)
  for index in range(7):
    elapsed = index * .05
    d_rel = max(0.5, distance + (speed - 25.) * elapsed)
    output = controller.update(.05 + elapsed, 25., (point,), model_with_lead(d_rel, 0.1, speed))
    radar = log.RadarState.new_message()
    # Match the real publisher's explicit empty-lead fallback when the visual
    # target has passed below its valid minimum range.
    radar.leadOne = output.lead_one or {"status": False}
    if output.lead_two is not None:
      radar.leadTwo = output.lead_two
    mpc, _ = run_update(radar_state=radar)
    assert np.isfinite(mpc.params).all()
    assert np.isfinite(mpc.yref).all()
    assert mpc.source in ("lead0", "lead1", "cruise", "e2e")
    if d_rel > .5:
      assert output.lead_one is not None
      assert output.lead_one["radarTrackId"] == (281 if confirm and elapsed < .20 - 1e-6 else -1)
    else:
      # The existing matcher can now choose radar without a valid visual lead.
      # Confirmation must not fabricate a visual target below the minimum.
      assert output.lead_one is None or output.lead_one["radar"]


def seed_vision_transition_front(controller):
  point = Point(281, 50.0, 0.1, trackState=2)
  output = controller.update(0.0, 25.0, (point,), model_with_lead(50.0, 0.1, 25.0))
  assert output.lead_one["radarTrackId"] == 281
  return point


@pytest.mark.parametrize("mode", (1, 2, 3))
@pytest.mark.parametrize("period", (0.025, 0.05, 0.10))
def test_healthy_front_switches_after_200ms_of_consistent_vision(mode, period):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode, cut_in_sensitivity=0)
  point = seed_vision_transition_front(controller)
  for index in range(round(0.30 / period) + 1):
    elapsed = index * period
    output = controller.update(period + elapsed, 25.0, (point,),
                               model_with_lead(30.0 - 15.0 * elapsed, 0.1, 10.0))
    if elapsed < 0.20 - 1e-6:
      assert output.lead_one["radarTrackId"] == 281
      assert output.lead_one["dRel"] == pytest.approx(50.0)
    else:
      assert output.lead_one["radarTrackId"] == -1
      assert output.lead_one["dRel"] == pytest.approx(30.0 - 15.0 * elapsed)


@pytest.mark.parametrize("flash_frames", (1, 2, 3))
def test_short_visual_outlier_keeps_current_radar_kinematics(flash_frames):
  controller = DPathRadarController(confirm_vision_transition=True, cut_in_sensitivity=0)
  point = seed_vision_transition_front(controller)
  for index in range(1, 12):
    time_s = index * 0.05
    current = replace(point, d_rel=50.0 - time_s, v_rel=-1.0, a_lead=-0.4)
    model = model_with_lead(30.0, 0.1, 10.0) if index <= flash_frames else model_with_lead(current.d_rel, 0.1, 24.0)
    output = controller.update(time_s, 25.0, (current,), model)
    assert output.lead_one["radarTrackId"] == 281
    assert output.lead_one["dRel"] == pytest.approx(current.d_rel)
    assert output.lead_one["vLead"] == pytest.approx(24.0)
    assert output.lead_one["aLeadK"] == pytest.approx(-0.4)


@pytest.mark.parametrize("change", ("distance", "velocity", "lateral", "model_ego_velocity"))
def test_visual_discontinuities_restart_transition_confirmation(change):
  controller = DPathRadarController(confirm_vision_transition=True, cut_in_sensitivity=0)
  point = seed_vision_transition_front(controller)
  for index in range(1, 20):
    time_s = index * 0.05
    model = model_with_lead(30.0, 0.0, 10.0)
    if index % 2:
      if change == "distance":
        model.leadsV3[0].x = (21.52,)
      elif change == "velocity":
        model.leadsV3[0].v = (16.0,)
      elif change == "lateral":
        model.leadsV3[0].y = (-0.9,)
      else:
        model.velocity = SimpleNamespace(x=(19.0,))
    output = controller.update(time_s, 25.0, (point,), model)
    assert output.lead_one["radarTrackId"] == 281
  for index in range(20, 25):
    output = controller.update(index * 0.05, 25.0, (point,), model_with_lead(30.0, 0.0, 10.0))
    assert output.lead_one["radarTrackId"] == (281 if index < 24 else -1)


@pytest.mark.parametrize("change", (
  "missing", "unmeasured", "new_id", "range_jump", "lateral_jump", "speed_jump",
  "off_path", "gap", "reverse_time", "invalid_path", "vision_mode",
))
def test_vision_transition_does_not_retain_broken_radar(change):
  controller = DPathRadarController(confirm_vision_transition=True, cut_in_sensitivity=0)
  point = seed_vision_transition_front(controller)
  model = model_with_lead(30.0, 0.0, 10.0)
  assert controller.update(0.05, 25.0, (point,), model).lead_one["radarTrackId"] == 281
  time_s = 0.1
  points = (point,)
  if change == "missing":
    points = ()
  elif change == "unmeasured":
    points = (replace(point, measured=False),)
  elif change == "new_id":
    points = (replace(point, track_id=282),)
  elif change == "range_jump":
    points = (replace(point, d_rel=55.0),)
  elif change == "lateral_jump":
    points = (replace(point, y_rel=-0.7),)
  elif change == "speed_jump":
    points = (replace(point, v_rel=4.0),)
  elif change == "off_path":
    model.position.y = (0.0, 4.0)
    model.leadsV3[0].y = (1.2,)
  elif change == "gap":
    time_s = 0.25
  elif change == "reverse_time":
    time_s = 0.04
  elif change == "invalid_path":
    controller.update(0.075, 25.0, (point,), SimpleNamespace(position=SimpleNamespace(x=(), y=())))
  else:
    controller.enable_radar_tracks = -2
  output = controller.update(time_s, 25.0, points, model)
  assert output.lead_one is not None and output.lead_one["radarTrackId"] == -1


def test_repeated_model_time_does_not_confirm_vision_transition():
  controller = DPathRadarController(confirm_vision_transition=True, cut_in_sensitivity=0)
  point = seed_vision_transition_front(controller)
  model = model_with_lead(30.0, 0.0, 10.0)
  for _ in range(20):
    assert controller.update(0.05, 25.0, (point,), model).lead_one["radarTrackId"] == 281
  for t in (0.10, 0.15, 0.20):
    assert controller.update(t, 25.0, (point,), model).lead_one["radarTrackId"] == 281
  assert controller.update(0.25, 25.0, (point,), model).lead_one["radarTrackId"] == -1


@pytest.mark.parametrize("mode", (2, 3))
@pytest.mark.parametrize("x_std", (2.0, 0.0, -10.0, float("nan"), float("inf")))
def test_moving_front_releases_when_vision_range_is_precise_or_invalid(mode, x_std):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode, cut_in_sensitivity=0)
  point = seed_moving_range_front(controller)
  for index in range(1, 7):
    output = controller.update(
      index * 0.05, 11.7, (replace(point, d_rel=89.4 + index * 0.25),),
      moving_range_model(79.4 + index * 0.25, x_std=x_std),
    )
    if index == 1:
      assert output.lead_one["radarTrackId"] == 35
  assert output.lead_one["radarTrackId"] == -1


@pytest.mark.parametrize("mode", (2, 3))
@pytest.mark.parametrize("radar_d,vision_d", ((90.0, 73.0), (40.0, 30.0)))
def test_uncertain_range_cannot_hide_much_nearer_or_close_visual_car(mode, radar_d, vision_d):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode, cut_in_sensitivity=0)
  point = seed_moving_range_front(controller, d_rel=radar_d)
  for index in range(1, 6):
    output = controller.update(
      index * 0.05, 11.7, (replace(point, d_rel=radar_d + index * 0.25),),
      moving_range_model(vision_d, x_std=30.0),
    )
    if index < 5:
      assert output.lead_one["radarTrackId"] == 35
  assert output.lead_one["radarTrackId"] == -1
  assert output.lead_one["dRel"] == pytest.approx(vision_d)


@pytest.mark.parametrize("mode", (2, 3))
def test_moving_front_range_retention_releases_for_stopped_visual_lead(mode):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode, cut_in_sensitivity=0)
  previous = seed_moving_range_front(controller)
  for index in range(1, 6):
    time_s = index * 0.05
    output = controller.update(
      time_s, 11.7, (replace(previous, d_rel=89.4 + 5.0 * time_s),),
      moving_range_model(80.235 - 11.7 * time_s, velocity=0.0),
    )
    if index < 5:
      assert output.lead_one["radarTrackId"] == 35
  assert output.lead_one["radarTrackId"] == -1


@pytest.mark.parametrize("mode", (2, 3))
def test_moving_range_retention_does_not_block_new_nearer_radar_match(mode):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode, cut_in_sensitivity=0)
  previous = seed_moving_range_front(controller)
  output = controller.update(
    0.05, 11.7, (
      replace(previous, d_rel=89.65),
      Point(36, 79.0, -0.16, v_rel=0.8, trackState=2),
    ), moving_range_model(79.0),
  )
  assert output.lead_one["radarTrackId"] == 36
  assert output.lead_one["dRel"] == pytest.approx(79.0)


@pytest.mark.parametrize("mode", (2, 3))
@pytest.mark.parametrize("change", (
  "precise_range", "farther_object", "close_visual_car", "position_jump", "lateral_jump",
  "speed_jump", "unmeasured", "gap", "missed_match", "invalid_path", "legacy_mode",
))
def test_stationary_range_hold_cannot_cover_a_new_or_contradictory_object(mode, change):
  controller = DPathRadarController(confirm_vision_transition=True, enable_radar_tracks=mode, cut_in_sensitivity=0)
  seed_stationary(controller)
  time_s = 1.0
  point, model = approach_frame(time_s, distance_error=14.0, x_std=2.0 if change == "precise_range" else 20.0)
  if change == "farther_object":
    model.leadsV3[0].x = (point.d_rel - 30.0 + 1.52,)
  elif change == "close_visual_car":
    point = replace(point, d_rel=40.0)
    model.leadsV3[0].x = (30.0 + 1.52,)
  elif change == "position_jump":
    point = replace(point, d_rel=point.d_rel + 4.0)
  elif change == "lateral_jump":
    point = replace(point, y_rel=-1.4)
  elif change == "speed_jump":
    point = replace(point, v_rel=-20.9)
  elif change == "unmeasured":
    point = replace(point, measured=False)
  elif change == "gap":
    time_s = 1.2
  elif change in ("missed_match", "invalid_path", "legacy_mode"):
    empty = model_with_lead(90.0, 0.0, 19.0)
    if change == "invalid_path":
      empty.position.x = ()
      empty.position.y = ()
    if change == "legacy_mode":
      controller.enable_radar_tracks = -1
    controller.update(1.0, 24.0, (), empty)
    controller.enable_radar_tracks = mode
    time_s = 1.05
  output = controller.update(time_s, 24.0, (point,), model, yaw_rate_rad_s=0.04)
  if change in ("precise_range", "farther_object"):
    assert output.lead_one["radarTrackId"] == 53
    for index in range(1, 5):
      next_time = time_s + index * 0.05
      next_point = replace(point, d_rel=point.d_rel + point.v_rel * index * 0.05)
      model.leadsV3[0].x = (model.leadsV3[0].x[0] - 0.25,)
      output = controller.update(next_time, 24.0, (next_point,), model, yaw_rate_rad_s=0.04)
  assert output.lead_one is None or output.lead_one["radarTrackId"] != 53
