from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.ui.pv5_manual_bsd import ManualBsd


class Inputs(dict):
  def __init__(self):
    super().__init__(
      carParams=NS(carFingerprint='KIA_PV5', notCar=False),
      carState=NS(canValid=True, gearShifter='drive', standstill=False, vEgo=10.,
                  leftBlinker=True, rightBlinker=False, pv5OemBlindspotValid=True, pv5OemBlindspotLeft=True,
                  pv5OemBlindspotRight=False, pv5OemBlindspotMonoTime=1_000_000_000, cruiseState=NS(enabled=False)),
      carControl=NS(latActive=False, longActive=False),
      selfdriveState=NS(enabled=False, active=False),
    )
    self.recv_time = dict.fromkeys(self, 1.)
    self.valid = dict.fromkeys(self, True)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('first', ['blinker', 'blindspot'])
def test_one_tone_per_signal_in_either_order(side, first):
  sm, gate = Inputs(), ManualBsd()
  gate.enabled = True
  cs = sm['carState']
  cs.leftBlinker = cs.rightBlinker = cs.pv5OemBlindspotLeft = cs.pv5OemBlindspotRight = False
  signal = f'{side}Blinker'
  detected = f'pv5OemBlindspot{side.title()}'
  a, b = (signal, detected) if first == 'blinker' else (detected, signal)
  def check(t):
    sm.recv_time.update(dict.fromkeys(sm, t))
    cs.pv5OemBlindspotMonoTime = int(t * 1e9)
    return gate.update(sm, t, can_now_ns=int(t * 1e9))
  setattr(cs, a, True)
  assert not check(1.)
  setattr(cs, b, True)
  assert check(1.1)
  assert not check(1.2)
  setattr(cs, signal, False)
  assert not check(1.3)
  setattr(cs, signal, True)
  assert not check(1.6)
  setattr(cs, signal, False)
  assert not check(2.6)
  setattr(cs, signal, True)
  assert check(2.7)


@pytest.mark.parametrize('service,field,value', [
  ('carParams','carFingerprint','OTHER'), ('carParams','notCar',True),
  ('carControl','latActive',True), ('carControl','longActive',True), ('selfdriveState','enabled',True),
  ('selfdriveState','active',True), ('carState','canValid',False), ('carState','standstill',True),
  ('carState','gearShifter','reverse'), ('carState','vEgo',0), ('carState','rightBlinker',True),
  ('carState','pv5OemBlindspotLeft',False), ('carState','pv5OemBlindspotValid',False),
  ('carState','pv5OemBlindspotMonoTime',849_999_999), ('carState','pv5OemBlindspotMonoTime',1_000_000_001),
])
def test_ineligible_and_fused_inputs(service, field, value):
  sm, gate = Inputs(), ManualBsd()
  gate.enabled = True
  sm['carState'].leftBlindspot = sm['carState'].leftBlindspotOem = True
  setattr(sm[service],field,value)
  assert not gate.update(sm, 1., can_now_ns=1_000_000_000)


def test_cruise_stale_mute_and_default_off():
  sm, gate = Inputs(), ManualBsd()
  assert not gate.update(sm, 1., can_now_ns=1_000_000_000)
  gate.enabled = True
  sm.recv_time['carControl'] = 0.
  assert not gate.update(sm, 1., can_now_ns=1_000_000_000)
  sm.recv_time['carControl'] = 1.
  sm['carState'].cruiseState.enabled = True
  assert not gate.update(sm, 1., can_now_ns=1_000_000_000)
  sm['carState'].cruiseState.enabled = False
  gate.gain = 0
  assert not gate.update(sm, 1., can_now_ns=1_000_000_000)
