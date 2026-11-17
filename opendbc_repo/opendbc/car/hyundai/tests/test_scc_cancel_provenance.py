"""Producer provenance, serialized transport and final wire-mode interaction."""
import ast
import ctypes
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from opendbc.car import structs
from opendbc.car.hyundai import scc_decel
from opendbc.car.hyundai.tests.test_scc_decel import cp, T
from opendbc.car.hyundai.tests.test_scc_gas_override import override_fixture, send_override, panda_native


def produce(cc, cs, config):
  tree = ast.parse((Path(__file__).resolve().parents[5] / 'openpilot/selfdrive/controls/controlsd.py').read_text())
  publish = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'publish')
  statements = [n for n in publish.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
                and ast.unparse(n.targets[0]) in ('CC.cruiseControl.cancel', 'CC.cruiseControl.pv5StockCancelOnly')]
  assert len(statements) == 2
  exec(compile(ast.Module(body=statements, type_ignores=[]), 'actual_publish_assignments', 'exec'),
       {'CC': cc, 'CS': cs.out, 'self': NS(CP=config), 'stock_cancel_only': scc_decel.stock_cancel_only})


def setup():
  cc, cs = override_fixture()
  cs.CP = cp(pcmCruise=False)
  cs.out.cruiseState.enabled = True
  cs.out.buttonEvents = []
  return cc, cs


def test_feedback_cycle_is_broken_with_zero_wire_demand():
  cc, cs = setup()
  modes = []
  for stock_enabled in [True, False] * 25:
    cs.out.cruiseState.enabled = stock_enabled
    produce(cc, cs, cs.CP)
    assert cc.cruiseControl.cancel == stock_enabled  # original cancel remains intact
    wire = structs.CarControl.new_message(enabled=cc.enabled, longActive=cc.longActive)
    wire.cruiseControl.override = True
    wire.cruiseControl.cancel = cc.cruiseControl.cancel
    wire.cruiseControl.pv5StockCancelOnly = cc.cruiseControl.pv5StockCancelOnly
    with structs.CarControl.from_bytes(wire.to_bytes()) as decoded:
      _, packet, value = send_override(decoded, cs)
    modes.append(packet['ACCMode'])
    assert value == 0 and all(packet[n] == 0 for n in ('aReqRaw','aReqValue','StopReq','AccelLimitBandLower','AccelLimitBandUpper'))
  assert modes == [2] * 50


@pytest.mark.parametrize('button', ['cancel', 'mainCruise'])
@pytest.mark.parametrize('pressed', [True, False])
def test_actual_button_cannot_be_hidden_by_old_enabled_or_provenance(button, pressed):
  cc, cs = setup()
  produce(cc, cs, cs.CP)
  assert cc.cruiseControl.pv5StockCancelOnly
  cs.out.buttonEvents = [NS(type=button, pressed=pressed)]
  assert not scc_decel.gas_override_allowed(cc, cs, T, 0)
  _, packet, _ = send_override(cc, cs)
  assert packet['ACCMode'] == 0
  produce(cc, cs, cs.CP)
  assert not cc.cruiseControl.pv5StockCancelOnly


@pytest.mark.parametrize('fault', ['disabled','brake','stale','bad_can','gear','unmarked','old_sender','pcm','other_car','not_car'])
def test_provenance_never_overrides_safety_or_unmarked_cancel(fault):
  cc, cs = setup(); produce(cc, cs, cs.CP)
  if fault == 'disabled': cc.enabled = False
  if fault == 'brake': cs.out.brakePressed = True
  if fault == 'stale': cs.cp.ts_nanos['TCS']['DriverBraking'] = T - 100_000_001
  if fault == 'bad_can': cs.out.canValid = False
  if fault == 'gear': cs.out.gearShifter = 'park'
  if fault == 'unmarked': cc.cruiseControl.pv5StockCancelOnly = False
  if fault == 'old_sender': del cc.cruiseControl.pv5StockCancelOnly
  if fault == 'pcm': cs.CP.pcmCruise = True
  if fault == 'other_car': cs.CP.carFingerprint = 'OTHER'
  if fault == 'not_car': cs.CP.notCar = True
  assert not scc_decel.gas_override_allowed(cc, cs, T, 0)
  _, packet, _ = send_override(cc, cs)
  assert packet['ACCMode'] == packet['aReqRaw'] == packet['aReqValue'] == packet['StopReq'] == 0


@pytest.mark.parametrize('fault', ['disabled','pcm','other_car','not_car'])
def test_producer_does_not_tag_other_cancel_domains(fault):
  cc, cs = setup()
  if fault == 'disabled': cc.enabled = False
  if fault == 'pcm': cs.CP.pcmCruise = True
  if fault == 'other_car': cs.CP.carFingerprint = 'OTHER'
  if fault == 'not_car': cs.CP.notCar = True
  produce(cc, cs, cs.CP)
  assert not cc.cruiseControl.pv5StockCancelOnly


def test_tagged_system_cancel_passes_real_panda_but_driver_cancel_does_not_rearm(panda_native):
  cc, cs = setup(); produce(cc, cs, cs.CP)
  lib = panda_native; lib.alt2_test_init(45); lib.pc_clear_controls()
  msg, packet, _ = send_override(cc, cs)
  data = (ctypes.c_uint8 * len(msg[1])).from_buffer_copy(msg[1])
  assert packet['ACCMode'] == 2
  assert lib.alt2_test_tx(msg[0],msg[2],len(msg[1]),data,1000)&1
  assert lib.pc_controls_allowed()
  cs.out.buttonEvents = [NS(type='cancel',pressed=True)]
  lib.pc_clear_controls()
  msg,packet,_ = send_override(cc,cs)
  data = (ctypes.c_uint8 * len(msg[1])).from_buffer_copy(msg[1])
  assert packet['ACCMode']==0
  assert lib.alt2_test_tx(msg[0],msg[2],len(msg[1]),data,21000)&1
  assert not lib.pc_controls_allowed()


@pytest.mark.parametrize('button',['cancel','mainCruise'])
@pytest.mark.parametrize('pressed',[True,False])
def test_cancel_latches_until_disabled_observed_even_if_control_message_is_delayed(button,pressed):
  cc,cs=setup();produce(cc,cs,cs.CP);guard=scc_decel.GasOverrideAdmission()
  assert guard.update(cc,cs,T,0)
  cs.out.buttonEvents=[NS(type=button,pressed=pressed)]
  assert not guard.update(cc,cs,T,0)
  cs.out.buttonEvents=[]
  for i in range(10):
    cs.out.cruiseState.enabled=bool(i%2);produce(cc,cs,cs.CP)
    assert not guard.update(cc,cs,T,0)
  cc.enabled=False
  assert not guard.update(cc,cs,T,0)
  cc.enabled=True;produce(cc,cs,cs.CP)
  assert guard.update(cc,cs,T,0)


def test_controller_samples_cancel_each_tick_before_half_rate_scc_send():
  path=Path(__file__).resolve().parents[1]/'carcontroller.py'
  tree=ast.parse(path.read_text())
  cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CarController')
  update=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='update')
  assert 'scc_override_admission.update' in ast.unparse(update.body[0])
  source=ast.unparse(update)
  assert 'gas_override_allowed=self.scc_decel_enabled and (not scc_allowed) and scc_gas_allowed' in source
