from types import SimpleNamespace as NS

import pytest

from hud_bsd import warning_sides


def snapshot():
  return 10., {'sent': 100., 'received': {'carState': 99.95},
               'valid': {'carState': True}, 'alive': {'carState': True}}


@pytest.mark.parametrize('turn_left,turn_right,bsd_left,bsd_right,expected', [
  (True,False,True,False,(True,False)),
  (False,True,False,True,(False,True)),
  (True,False,False,True,(False,False)),
  (False,False,True,True,(False,False)),
  (True,True,True,True,(True,True)),
])
def test_warning_requires_turn_and_bsd_on_the_same_side(turn_left,turn_right,bsd_left,bsd_right,expected):
  state=NS(left_signal=turn_left,right_signal=turn_right,left_blindspot=bsd_left,right_blindspot=bsd_right)
  assert warning_sides(state,snapshot(),10.1)==expected


@pytest.mark.parametrize('change', ['missing','stale_snapshot','stale_car','sum_stale','invalid','not_alive','nan','bad_type'])
def test_stale_invalid_or_missing_data_never_keeps_a_red_warning(change):
  state=NS(left_signal=True,right_signal=True,left_blindspot=True,right_blindspot=True)
  stamp,data=snapshot();now=10.1
  if change=='missing':assert warning_sides(state,None,now)==(False,False);return
  if change=='stale_snapshot':now=10.4
  elif change=='stale_car':data['received']['carState']=99.6
  elif change=='sum_stale':data['received']['carState']=99.75
  elif change=='invalid':data['valid']['carState']=False
  elif change=='not_alive':data['alive']['carState']=False
  elif change=='nan':data['sent']=float('nan')
  elif change=='bad_type':data['sent']='100'
  assert warning_sides(state,(stamp,data),now)==(False,False)
