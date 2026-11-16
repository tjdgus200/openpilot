"""Pre-entry blending must never slow an admitted urgent OEM/base request."""
import math

import pytest

from opendbc.car.hyundai.scc_decel import StockDecelAssist, Observation, ENTRY_ACCEL, ENTRY_BLEND_START

T = 10_000_000_000
DT = 10_000_000


def obs(t, raw, value=None):
  return Observation(t, raw, raw if value is None else value, True)


@pytest.mark.parametrize('base', [-3., -1., 0., 1.])
@pytest.mark.parametrize('raw', [-2., -2.01, -3.5, -8.])
@pytest.mark.parametrize('warm', [False, True])
def test_urgent_request_same_tick_with_or_without_preparation(base, raw, warm):
  c = StockDecelAssist()
  if warm:
    for i in range(40): c.update(base, obs(T+i*DT, -1.8), T+i*DT, True)
  t = T+40*DT
  r = c.update(base, obs(t, raw, .2), t, True)
  assert r.active
  assert r.accel <= max(-4., min(base, raw)) + 1e-9
  assert r.source_stamp == t


@pytest.mark.parametrize('raw', [-1.5, -1., -.1, 0., 1.])
@pytest.mark.parametrize('base', [-3., -.5, 0., 1., 3.])
def test_weak_oem_request_does_not_start_assistance(raw, base):
  c = StockDecelAssist()
  for i in range(100):
    r = c.update(base, obs(T+i*DT, raw), T+i*DT, True)
    assert not r.active and r.accel == base


def test_gradual_oem_approach_has_no_threshold_step():
  c = StockDecelAssist(); last = -.5; entry_change = None
  for i in range(151):
    raw = -1.25-i*.01
    r = c.update(-.5, obs(T+i*DT, raw), T+i*DT, True)
    assert r.accel <= -.5
    if abs(raw-ENTRY_ACCEL)<1e-8:entry_change=abs(r.accel-last)
    if raw <= ENTRY_ACCEL:assert r.accel <= raw+1e-9
    last=r.accel
  assert entry_change < .04


def test_cold_pre_entry_and_stronger_base_have_separate_limits():
  c = StockDecelAssist()
  r = c.update(0., obs(T,-1.99), T, True)
  assert -.05 <= r.accel < 0.
  r = c.update(-3., obs(T+DT,-1.99), T+DT, True)
  assert r.accel == -3.


@pytest.mark.parametrize('invalid', ['driver', 'stale', 'future', 'counter_or_crc', 'nan', 'clock_reverse'])
def test_preparation_drops_immediately_on_existing_admission_failure(invalid):
  c = StockDecelAssist(); c.update(0., obs(T,-1.9), T, True)
  t=T+DT; s=obs(t,-1.9); admitted=True
  if invalid=='driver':admitted=False
  elif invalid=='stale':s=obs(t-100_000_001,-1.9)
  elif invalid=='future':s=obs(t+1,-1.9)
  elif invalid=='counter_or_crc':s=Observation(t,-1.9,-1.9,False)
  elif invalid=='nan':s=obs(t,math.nan)
  else:t=T-1;s=obs(t,-1.9)
  r=c.update(.2,s,t,admitted)
  assert not r.active and r.accel==.2 and c.last_accel is None


def test_aborted_preparation_releases_without_a_latch_or_acceleration_jump():
  c=StockDecelAssist()
  for i in range(50):r=c.update(.2,obs(T+i*DT,-1.8),T+i*DT,True)
  last=r.accel
  for i in range(50,400):
    r=c.update(.2,obs(T+i*DT,.1),T+i*DT,True)
    assert r.accel <= last+.01+1e-9
    last=r.accel
  assert r.accel==.2 and not r.active and c.phase=='normal'

@pytest.mark.parametrize('raw', [-2., -2.5, -3.8])
@pytest.mark.parametrize('base', [.5, -3.])
def test_preparation_to_urgent_request_reaches_CAN_without_extra_delay(monkeypatch, raw, base):
  from types import SimpleNamespace as NS
  from opendbc.car import structs
  from opendbc.can import CANParser
  from opendbc.car.hyundai.hyundaicanfd import create_acc_control_scc2
  from opendbc.car.hyundai.stopping import CanfdStopping
  from opendbc.car.hyundai.tests.test_scc_decel import (
    controller_fixture, admission_fixture, parser_fixture, receive, packet_fixture, decoded_packet,
  )

  controller, _ = controller_fixture(monkeypatch)
  cc, cs = admission_fixture()
  cc.actuators = NS(longControlState=structs.CarControl.Actuators.LongControlState.pid)
  cs.out.carrotCruise = 0
  cs.scc_control = {'COUNTER': 1, 'InfoDisplay': 0}
  cs.cp = CANParser('hyundai_canfd_generated', [('TCS', 50)], 0)
  packer, cs.cp_cam = parser_fixture()
  selected = .5
  for k in range(1, 24):
    now = T + (k-1)*20_000_000
    cs.cp.update([(now, [packer.make_can_msg('TCS', 0, {'COUNTER': k, 'DriverBraking': 0})])])
    receive(packer, cs.cp_cam, k, now, aReqRaw=raw if k==23 else -1.8, aReqValue=.2)
    previous = selected
    selected, allowed = controller._apply_scc_decel(cc, cs, now, base)
  assert allowed and selected == pytest.approx(min(base, raw))
  body, hud, jerk = packet_fixture()
  msg, _ = create_acc_control_scc2(
    packer, NS(ECAN=0), True, previous, selected, False, False, 40., hud, jerk,
    body, CanfdStopping(), longitudinal_allowed=allowed,
  )
  d = decoded_packet(packer, msg)
  assert d['ACCMode'] == 1
  assert d['aReqRaw'] == pytest.approx(min(base, raw), abs=.011)
  # Preserve the existing aReqValue slew and wire checksum, not just arbitration metadata.
  from opendbc.car.hyundai.hyundaicanfd import hkg_can_fd_checksum
  assert int.from_bytes(msg[1][:2], 'little') == hkg_can_fd_checksum(416, None, bytearray(msg[1]))
