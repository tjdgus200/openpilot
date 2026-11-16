"""Nonurgent mode-3 radar return may not defer closer/urgent or uncertain targets."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.carrot.radar_motion.controller import DPathRadarController
from openpilot.selfdrive.carrot.radar_motion.primary import RadarPointSnapshot, VisionRadarMatch

PATH = ((0., 0.), (200., 0.))
MODEL = SimpleNamespace(meta=SimpleNamespace(laneChangeState="off"))


def point(track_id=38, source="frontRadar", **changes):
  base = RadarPointSnapshot(track_id=track_id, source=source, d_rel=15., y_rel=1.3, v_rel=2.,
                            a_rel=0., yv_rel=0., v_lead=12., a_lead=1., j_lead=0., measured=True)
  return replace(base, **changes)


def match(p):
  return VisionRadarMatch(p, .9, 1., p.y_rel)


def inputs(t):
  near = point(d_rel=15.+2.*(t-1.))
  scc = replace(near, track_id=0, source="scc", y_rel=0., d_rel=near.d_rel-.4)
  far = point(track_id=33, d_rel=31.+3.*(t-1.), y_rel=0., v_rel=3., v_lead=13., a_lead=.7)
  return near, scc, far


def call(c, t, candidate=None, points=None, *, forced=None, urgent=False):
  near, scc, far = inputs(t)
  return c._confirm_farther_after_scc(candidate or match(far), forced, tuple(points or (near, scc, far)),
                                     PATH, None, MODEL, t, urgent)


def seeded():
  c = DPathRadarController(enable_radar_tracks=3, pv5_refinements=True)
  near, scc, far = inputs(1.)
  m = match(near)
  assert call(c, 1., m, forced=m) is m
  return c


@pytest.mark.parametrize("period", [.025, .05, .10])
def test_current_scc_body_is_refreshed_until_consistent_farther_return(period):
  c = seeded()
  for i in range(round(.2/period)+1):
    t = 1.+period+i*period
    near, _, far = inputs(t)
    m = match(far)
    out = call(c, t, m)
    if i*period < .2-1e-6:
      assert out.point.track_id == 38
      assert out.point.d_rel == pytest.approx(near.d_rel)
    else:
      assert out is m


def test_short_accept_reject_round_trip_keeps_fresh_near_body():
  c = seeded()
  assert call(c, 1.05).point.track_id == 38
  near, _, _ = inputs(1.10)
  m = match(near)
  assert call(c, 1.10, m, forced=m) is m
  assert call(c, 1.15).point.track_id == 38
  # Original SCC disappears. Do not preserve its last sample for the dwell.
  near, _, far = inputs(1.20)
  m = match(far)
  assert call(c, 1.20, m, (near, far)) is m
  assert c._scc_return_previous is None and c._scc_return_pending is None


@pytest.mark.parametrize("changes", [
  {"d_rel": 1.91}, {"d_rel": 14.}, {"d_rel": 16.9}, {"v_rel": -6.5},
  {"v_rel": -5.6}, {"a_lead": -1.0}, {"a_lead": -4.},
  {"source": "scc"}, {"source": "corner235"}, {"measured": False},
])
def test_closer_cutin_braking_rapid_closing_and_oem_targets_are_immediate(changes):
  c = seeded(); call(c, 1.05)
  far = replace(inputs(1.10)[2], **changes); m = match(far)
  assert call(c, 1.10, m) is m
  assert c._scc_return_pending is None


def test_independent_cutin_or_cutout_risk_bypasses_and_discards_hold():
  c=seeded(); call(c,1.05); m=match(inputs(1.10)[2])
  assert call(c,1.10,m,urgent=True) is m
  assert c._scc_return_previous is None


def test_strong_scc_fallback_does_not_wait_even_during_pending_return():
  c=seeded(); call(c,1.05)
  near=point(track_id=43,d_rel=1.91,v_rel=-2.5,a_lead=-3.45); m=match(near)
  assert call(c,1.10,m,forced=m) is m


@pytest.mark.parametrize("mode", [-1,0,1,2])
def test_other_modes_discard_history_and_preserve_original_choice(mode):
  c=seeded(); c.enable_radar_tracks=mode; m=match(inputs(1.05)[2])
  assert call(c,1.05,m) is m
  c.enable_radar_tracks=3
  assert call(c,1.10,m) is m


def test_non_pv5_is_exact_passthrough():
  c=seeded(); c.pv5_refinements=False; m=match(inputs(1.05)[2])
  assert call(c,1.05,m) is m


@pytest.mark.parametrize("t", [.95,1.151,float("nan"),float("inf")])
def test_reversed_discontinuous_or_invalid_time_discards_hold(t):
  c=seeded(); m=match(inputs(1.05)[2])
  near,scc,far=inputs(1.05)
  assert call(c,t,m,(near,scc,far)) is m
  assert c._scc_return_previous is None


@pytest.mark.parametrize("kind", ["missing_scc","missing_front","scc_distance","scc_speed","front_unmeasured","reused_id","range_jump","lateral_jump","speed_jump","beside_path"])
def test_uncertain_or_changed_support_cannot_retain_old_target(kind):
  c=seeded(); near,scc,far=inputs(1.05); m=match(far)
  if kind=="missing_scc": points=(near,far)
  elif kind=="missing_front": points=(scc,far)
  elif kind=="scc_distance": points=(near,replace(scc,d_rel=25.),far)
  elif kind=="scc_speed": points=(near,replace(scc,v_lead=20.),far)
  elif kind=="front_unmeasured": points=(replace(near,measured=False),scc,far)
  elif kind=="reused_id": points=(replace(near,track_id=39),scc,far)
  elif kind=="range_jump": points=(replace(near,d_rel=19.),replace(scc,d_rel=18.6),far)
  elif kind=="lateral_jump": points=(replace(near,y_rel=.4),scc,far)
  elif kind=="speed_jump": points=(replace(near,v_lead=16.),replace(scc,v_lead=16.),far)
  else: points=(replace(near,y_rel=3.),scc,far)
  assert call(c,1.05,m,points) is m


@pytest.mark.parametrize("which,field", [
  (which, field) for which in ("candidate", "scc", "near")
  for field in ("d_rel", "y_rel", "v_rel", "v_lead", "a_lead", "yv_rel")
  if not (which == "scc" and field in ("y_rel", "yv_rel"))
])
@pytest.mark.parametrize("value",[float("nan"),float("inf"),-float("inf")])
def test_invalid_numeric_state_never_causes_comfort_retention(which,field,value):
  # SCC lateral values are intentionally zeroed by the existing interface;
  # these fields are not independent input evidence for SCC association.
  c=seeded(); near,scc,far=inputs(1.05)
  if which=="candidate": far=replace(far,**{field:value})
  elif which=="scc": scc=replace(scc,**{field:value})
  else: near=replace(near,**{field:value})
  m=match(far)
  assert call(c,1.05,m,(near,scc,far)) is m


def test_far_candidate_reid_and_repeated_time_do_not_complete_confirmation():
  c=seeded()
  for _ in range(20): assert call(c,1.05).point.track_id==38
  for i in range(1,8):
    t=1.05+.05*i; m=match(replace(inputs(t)[2],track_id=33+i%2))
    assert call(c,t,m).point.track_id==38


def test_missing_path_clears_retention_history():
  c=seeded(); call(c,1.05)
  c.update(time_s=1.10,v_ego=10.,radar_points=(),model=SimpleNamespace(position=SimpleNamespace(x=[],y=[])))
  assert c._scc_return_previous is None and c._scc_return_pending is None

def test_new_scc_physical_target_releases_pending_return_immediately():
  c = seeded(); call(c, 1.05)
  near, _, far = inputs(1.10)
  scc = replace(far, track_id=0, source="scc", y_rel=0., d_rel=far.d_rel-.3, a_lead=-3.45)
  m = match(far)
  assert call(c, 1.10, m, (near, scc, far)) is m


def test_missing_primary_discards_history_without_fabricating_a_lead():
  c = seeded(); call(c, 1.05)
  assert c._confirm_farther_after_scc(None, None, inputs(1.10), PATH, None, MODEL, 1.10, False) is None
  assert c._scc_return_previous is None


def test_low_speed_old_lane_object_during_lane_change_is_not_retained():
  c = seeded()
  old = point(v_lead=2.)
  c._scc_return_previous = (1., old)
  near = replace(old, d_rel=15.1)
  scc = replace(near, source="scc", track_id=0, y_rel=0., d_rel=14.7)
  far = inputs(1.05)[2]; m=match(far)
  lane_change = SimpleNamespace(meta=SimpleNamespace(laneChangeState="laneChangeStarting"))
  assert c._confirm_farther_after_scc(m,None,(near,scc,far),PATH,None,lane_change,1.05,False) is m


@pytest.mark.parametrize("kind", ["range_gap", "ttc", "required_deceleration"])
def test_nonurgent_boundary_is_conservative(kind):
  c=seeded(); near,scc,far=inputs(1.05)
  if kind=="range_gap": far=replace(far,d_rel=near.d_rel+2.)
  elif kind=="ttc": far=replace(far,d_rel=30.,v_rel=-6.)
  else: far=replace(far,d_rel=25.,v_rel=-5.)
  m=match(far)
  assert call(c,1.05,m,(near,scc,far)) is m
