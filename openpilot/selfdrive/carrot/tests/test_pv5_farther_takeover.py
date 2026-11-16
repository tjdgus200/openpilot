"""No-delay contracts for PV5 target and OEM-deceleration transitions."""
from dataclasses import replace

import pytest

from openpilot.selfdrive.carrot.radar_motion.controller import DPathRadarController
from openpilot.selfdrive.carrot.radar_motion.primary import RadarPointSnapshot, VisionRadarMatch


def vision(**changes):
  return dict(status=True, radar=False, radarTrackId=-1, dRel=20., yRel=0., vRel=-1.8,
              vLead=.7, dPath=0., modelProb=.9, **changes)


def match(**changes):
  p = RadarPointSnapshot(track_id=39, source='frontRadar', d_rel=29., y_rel=0., v_rel=-2.5,
                         a_rel=0., yv_rel=0., v_lead=0., a_lead=0., j_lead=0., measured=True)
  return VisionRadarMatch(replace(p, **changes), .9, 1., 0.)


def seeded():
  c = DPathRadarController(pv5_refinements=True)
  assert c._confirm_farther_radar(None, vision(), 1.) is None
  return c


@pytest.mark.parametrize('period', [.025, .05, .10])
def test_only_consistent_farther_takeover_waits(period):
  c = seeded()
  for i in range(round(.2 / period) + 1):
    m = match()
    out = c._confirm_farther_radar(m, vision(), 1. + period + i * period)
    assert (out is m) == (i * period >= .2 - 1e-6)


def test_one_frame_farther_flash_does_not_create_round_trip():
  c = seeded()
  assert c._confirm_farther_radar(match(), vision(), 1.05) is None
  assert c._confirm_farther_radar(None, vision(), 1.10) is None
  assert c._confirm_farther_radar(match(), vision(), 1.15) is None


@pytest.mark.parametrize('changes', [
  {'d_rel': 5.}, {'d_rel': 19.9}, {'d_rel': 21.9},
  {'d_rel': 29., 'v_rel': -6.}, {'a_lead': -1.01},
  {'source': 'scc'}, {'source': 'corner235'}, {'measured': False},
])
def test_new_close_cutin_and_urgent_braking_never_wait(changes):
  c = seeded()
  c._confirm_farther_radar(match(), vision(), 1.05)
  m = match(**changes)
  assert c._confirm_farther_radar(m, vision(), 1.10) is m


@pytest.mark.parametrize('kind', ['missing', 'range_jump', 'lateral_jump', 'speed_jump', 'off_path', 'low_probability', 'time_gap', 'time_reverse', 'disabled', 'non_pv5'])
def test_weak_or_discontinuous_vision_cannot_hold_radar(kind):
  c = seeded(); v = vision(); t = 1.05
  if kind == 'missing': v = None
  elif kind == 'range_jump': v['dRel'] = 14.
  elif kind == 'lateral_jump': v['yRel'] = 2.
  elif kind == 'speed_jump': v['vLead'] = 5.
  elif kind == 'off_path': v['dPath'] = 1.1
  elif kind == 'low_probability': v['modelProb'] = .39
  elif kind == 'time_gap': t = 1.20
  elif kind == 'time_reverse': t = .95
  elif kind == 'disabled': c.enable_radar_tracks = 0
  else: c.pv5_refinements = False
  m = match()
  assert c._confirm_farther_radar(m, v, t) is m


def test_repeated_timestamps_and_changed_id_do_not_confirm():
  c = seeded()
  for _ in range(30):
    assert c._confirm_farther_radar(match(), vision(), 1.05) is None
  for i in range(1, 20):
    assert c._confirm_farther_radar(match(track_id=i % 2), vision(), 1.05 + .05*i) is None


@pytest.mark.parametrize('field', ['d_rel', 'y_rel', 'v_rel', 'v_lead', 'a_lead', 'yv_rel'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_uncertain_radar_values_never_enter_comfort_hold(field, value):
  c = seeded()
  m = match(**{field: value})
  assert c._confirm_farther_radar(m, vision(), 1.05) is m
