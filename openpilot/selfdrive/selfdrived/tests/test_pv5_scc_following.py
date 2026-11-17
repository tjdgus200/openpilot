"""Conservative warning-only downgrade; new hazards retain immediate urgency."""
from types import SimpleNamespace as NS
import pytest
from openpilot.common.pv5_scc_warning import SccWarning
from openpilot.selfdrive.selfdrived.tests.test_pv5_scc_warning import T, cp, state, output


def tick(w, seconds, *, distance=9.9, relative=-1.3, ego=6.55, track=45, valid=True, stamp_age=0, **changes):
  t=T+round(seconds*1e9)
  radar=NS(leadOne=NS(status=True,radar=True,radarTrackId=track,dRel=distance,vRel=relative))
  out=output(t,pv5SccWarningDistance=distance,pv5SccWarningRelSpeed=relative,**changes)
  return w.update(cp(),state(vEgo=ego),out,t,t,radar=radar,radar_stamp=t-stamp_age,radar_valid=valid)


def prime(w):
  for i in range(61):
    # Constant relative speed with a coherent decreasing distance.
    tick(w,i*.02,distance=11.46-i*.026,pv5SccDecelActive=False)


def test_recorded_stable_following_downgrades_only_with_positive_evidence():
  w=SccWarning();prime(w)
  cue=tick(w,1.22)
  assert cue.level==1 and not cue.audible
  assert tick(SccWarning(),1.22).level==2  # no history is never presumed safe


@pytest.mark.parametrize('changes', [
  {'track':46}, {'track':-1}, {'valid':False}, {'stamp_age':150_000_001},
  {'distance':1.9,'relative':-1.3},
  {'distance':8.5,'relative':-1.8,'ego':18.5},
  {'distance':9.9,'relative':-5.0},
  {'pv5SccWarningRaw':-3.0}, {'pv5SccWarningRaw':-3.5},
  {'pv5SccWarningBaseAccel':0.5,'accel':-2.0},
  {'pv5SccWarningVersion':0}, {'pv5SccWarningMonoTime':0},
])
def test_new_close_fast_strong_unknown_cases_remain_immediate(changes):
  w=SccWarning();prime(w)
  assert tick(w,1.22,**changes).level>=2


def test_new_danger_after_ordinary_following_has_no_extra_wait():
  w=SccWarning();prime(w)
  assert tick(w,1.22).level==1
  cue=tick(w,1.24,distance=1.9,relative=-3.,track=62)
  assert cue.level==3 and cue.audible


@pytest.mark.parametrize('fault',['same_stamp_changed','gap','backward','vision_only','target_mismatch'])
def test_invalid_or_mismatched_history_cannot_suppress_near_warning(fault):
  w=SccWarning();prime(w)
  if fault=='same_stamp_changed':
    assert tick(w,1.2,distance=9.91).level==2
  elif fault=='gap': assert tick(w,1.6).level==2
  elif fault=='backward': assert tick(w,1.0).level==2
  elif fault=='vision_only':
    t=T+1_220_000_000
    radar=NS(leadOne=NS(status=True,radar=False,radarTrackId=45,dRel=9.9,vRel=-1.3))
    assert w.update(cp(),state(vEgo=6.55),output(t,pv5SccWarningDistance=9.9,pv5SccWarningRelSpeed=-1.3),t,t,radar=radar,radar_stamp=t,radar_valid=True).level==2
  else:
    t=T+1_220_000_000
    radar=NS(leadOne=NS(status=True,radar=True,radarTrackId=45,dRel=9.9,vRel=-1.3))
    assert w.update(cp(),state(vEgo=6.55),output(t,pv5SccWarningDistance=7.5,pv5SccWarningRelSpeed=-1.3),t,t,radar=radar,radar_stamp=t,radar_valid=True).level==2
