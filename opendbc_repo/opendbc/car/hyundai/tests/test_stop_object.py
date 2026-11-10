from types import SimpleNamespace as NS

import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car import structs
from opendbc.car.hyundai.hyundaicanfd import create_acc_control_scc2
from opendbc.car.hyundai.stop_object import signal_stop_object_requested
from opendbc.car.hyundai.stopping import CanfdStopping
from opendbc.car.hyundai.tests.test_stopping import make_cs as make_stopping_cs
from opendbc.car.hyundai.values import CAR, HyundaiFlags


def make_cs():
  cs = make_stopping_cs()
  cs.CP = structs.CarParams(brand="hyundai", carFingerprint=CAR.KIA_PV5,
                           flags=int(HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC),
                           openpilotLongitudinalControl=True)
  return cs


def send(cs, controller, *, requested=True, stopping=True, enabled=True, override=False, hud=None):
  hud = NS(leadDistanceBars=2, signalStopObject=requested) if hud is None else hud
  msg, _ = create_acc_control_scc2(
    CANPacker("hyundai_canfd_generated"),
    NS(ECAN=0),
    enabled,
    -0.5,
    -0.5,
    stopping,
    override,
    30.0,
    hud,
    NS(carrot_cruise=0, jerk_u=2.0, jerk_l=1.0),
    cs,
    controller,
  )
  parser = CANParser("hyundai_canfd_generated", [("SCC_CONTROL", 50)], 0)
  parser.update([1_000_000_000, [msg]])
  return dict(parser.vl["SCC_CONTROL"]), msg[1]


def test_final_stop_object_adds_only_object_fields_and_crc():
  cs = make_cs()
  before = dict(cs.scc_control)
  old, old_bytes = send(cs, CanfdStopping(), requested=False)
  controller = CanfdStopping()
  new, new_bytes = send(cs, controller)
  assert controller.stop_object_active
  assert (new["ACCMode"], new["StopReq"], new["HUD_LEAD_INFO"]) == (1, 1, 2)
  assert new["ACC_ObjDist"] == 3.0
  assert new["ACC_ObjRelSpd"] == pytest.approx(-cs.out.vEgo)
  assert new["ACC_ObjLatPos"] == 0.0
  changed = {k for k in new if new[k] != old[k]}
  assert changed <= {"ACC_ObjDist", "ACC_ObjRelSpd", "CHECKSUM"}
  assert {"ACC_ObjDist", "ACC_ObjRelSpd"} <= changed
  assert old_bytes != new_bytes and cs.scc_control == before
  assert new["aReqRaw"] == new["aReqValue"] == 0.0


@pytest.mark.parametrize(
  "field,value",
  [
    ("gasPressed", True),
    ("brakePressed", True),
    ("parkingBrake", True),
    ("brakeHoldActive", True),
    ("canValid", False),
    ("gearShifter", "reverse"),
    ("vEgo", 1.0),
    ("vEgo", float("nan")),
  ],
)
def test_interlocks_do_not_emit_synthetic_object(field, value):
  cs = make_cs()
  setattr(cs.out, field, value)
  controller = CanfdStopping()
  values, _ = send(cs, controller)
  assert not controller.stop_object_active
  assert values["ACC_ObjDist"] != 3.0


@pytest.mark.parametrize("kwargs", [{"requested": False}, {"stopping": False}, {"enabled": False}, {"override": True}])
def test_object_releases_without_latching(kwargs):
  cs = make_cs()
  controller = CanfdStopping()
  send(cs, controller)
  assert controller.stop_object_active
  values, _ = send(cs, controller, **kwargs)
  assert not controller.stop_object_active and values["ACC_ObjDist"] == pytest.approx(204.6)


@pytest.mark.parametrize("which", ["leadOne", "leadTwo"])
def test_real_lead_wins_and_input_is_never_modified(which):
  cs = make_cs()
  lead = NS(status=True, dRel=8.0, vRel=-0.4, yRel=0.2)
  cs.radarState = NS(leadOne=None, leadTwo=None)
  setattr(cs.radarState, which, lead)
  controller = CanfdStopping()
  values, _ = send(cs, controller)
  assert not controller.stop_object_active
  assert values["ACC_ObjDist"] == 8.0 and values["ACC_ObjRelSpd"] == pytest.approx(-0.4)
  assert lead.dRel == 8.0 and lead.vRel == -0.4


def setup_request():
  cp = make_cs().CP
  cs = structs.CarState(canValid=True, gearShifter="drive")
  cc = structs.CarControl(enabled=True, longActive=True)
  plan = NS(shouldStop=True, xState=3, hasLead=False)
  radar = NS(leadOne=NS(status=False), leadTwo=NS(status=False))
  return cp, cs, cc, plan, radar


@pytest.mark.parametrize("xstate,expected", [(0, False), (1, False), (2, False), (3, True), (4, False), (5, True), (6, False)])
def test_request_only_for_signal_stopping_states(xstate, expected):
  cp, cs, cc, plan, radar = setup_request()
  plan.xState = xstate
  assert signal_stop_object_requested(cp, cs, cc, plan, radar, inputs_ready=True) == expected


@pytest.mark.parametrize("block", ["classic", "no_camera", "other_brand", "other_car", "not_car", "stock_long", "invalid", "lead", "off", "pedal", "no_stop"])
def test_request_admission_scope(block):
  cp, cs, cc, plan, radar = setup_request()
  ready = True
  if block == "classic":
    cp.flags = int(HyundaiFlags.CAMERA_SCC)
  elif block == "no_camera":
    cp.flags = int(HyundaiFlags.CANFD)
  elif block == "other_brand":
    cp.brand = "toyota"
  elif block == "other_car":
    cp.carFingerprint = CAR.KIA_EV6
  elif block == "not_car":
    cp.notCar = True
  elif block == "stock_long":
    cp.openpilotLongitudinalControl = False
  elif block == "invalid":
    ready = False
  elif block == "lead":
    radar.leadTwo.status = True
  elif block == "off":
    cc.longActive = False
  elif block == "pedal":
    cs.gasPressed = True
  elif block == "no_stop":
    plan.shouldStop = False
  assert not signal_stop_object_requested(cp, cs, cc, plan, radar, inputs_ready=ready)


@pytest.mark.parametrize("fingerprint", [CAR.KIA_EV6, CAR.KIA_EV9, CAR.HYUNDAI_CASPER_EV, ""])
def test_final_sender_rejects_other_vehicles_even_with_request(fingerprint):
  cs = make_cs()
  cs.CP.carFingerprint = fingerprint
  reference, reference_bytes = send(cs, CanfdStopping(), requested=False)
  controller = CanfdStopping()
  values, payload = send(cs, controller, requested=True)
  assert not controller.stop_object_active
  assert values == reference and payload == reference_bytes


@pytest.mark.parametrize("field", ["vEgo", "vEgoRaw", "fl", "fr", "rl", "rr"])
@pytest.mark.parametrize("value", [.70001, -.70001, float("nan"), float("inf"), -float("inf")])
def test_every_speed_sample_must_be_finite_and_in_low_speed_region(field, value):
  cs = make_cs()
  target = cs.out if field in ("vEgo", "vEgoRaw") else cs.out.wheelSpeeds
  setattr(target, field, value)
  controller = CanfdStopping()
  send(cs, controller)
  assert not controller.stop_object_active


def test_can_timeout_and_missing_scc_template_clear_previous_application():
  cs = make_cs()
  controller = CanfdStopping()
  send(cs, controller)
  assert controller.stop_object_active
  cs.out.canTimeout = True
  send(cs, controller)
  assert not controller.stop_object_active
  cs.out.canTimeout = False
  send(cs, controller)
  assert controller.stop_object_active
  cs.scc_control = None
  message, _ = create_acc_control_scc2(
    CANPacker("hyundai_canfd_generated"), NS(ECAN=0), True, -.5, -.5, True, False, 30.,
    NS(leadDistanceBars=2, signalStopObject=True), NS(carrot_cruise=0, jerk_u=2., jerk_l=1.), cs, controller,
  )
  assert message is None and not controller.stop_object_active


def test_schema_uses_upstream_wire_tags_with_default_off():
  cc = structs.CarControl.new_message()
  assert not cc.hudControl.signalStopObject and not cc.actuators.stopObjectActive
  assert structs.CarControl.HUDControl.schema.fields["signalStopObject"].proto.ordinal.explicit == 24
  assert structs.CarControl.Actuators.schema.fields["stopObjectActive"].proto.ordinal.explicit == 11


@pytest.mark.parametrize('xstate', (3, 5))
def test_published_request_reaches_scc_and_clears_when_inputs_expire(xstate):
  from opendbc.car.hyundai.stopping import CanfdStopping
  from openpilot.selfdrive.controls.tests.test_controlsd_publish import publish_fixture

  flags = int(HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC)
  control, sent, publish = publish_fixture('hyundai', flags, True)
  control.sm['longitudinalPlan'].xState = xstate
  publish()
  cc = sent['carControl'].carControl
  cs = make_cs()
  cs.CP = control.CP
  controller = CanfdStopping()
  before = dict(cs.scc_control)
  values, _ = send(cs, controller, hud=cc.hudControl, enabled=cc.enabled,
                   stopping=cc.actuators.longControlState == 'stopping')
  assert controller.stop_object_active and values['ACC_ObjDist'] == 3.
  assert values['aReqRaw'] == values['aReqValue'] == 0.
  assert cs.scc_control == before
  assert not control.sm['radarState'].leadOne.status
  assert not control.sm['radarState'].leadTwo.status

  control.sm.recv_frame['longitudinalPlan'] = control.sm.frame-21
  publish()
  expired = sent['carControl'].carControl
  values, _ = send(cs, controller, hud=expired.hudControl)
  assert not controller.stop_object_active and values['ACC_ObjDist'] != 3.


def test_real_lead_preempts_the_full_publish_to_scc_path():
  from opendbc.car.hyundai.stopping import CanfdStopping
  from openpilot.selfdrive.controls.tests.test_controlsd_publish import publish_fixture

  flags = int(HyundaiFlags.CANFD | HyundaiFlags.CAMERA_SCC)
  control, sent, publish = publish_fixture('hyundai', flags, True)
  control.sm['radarState'].leadOne = {'status': True, 'dRel': 8., 'vRel': -.4, 'yRel': .2}
  publish()
  cc = sent['carControl'].carControl
  assert not cc.hudControl.signalStopObject
  cs = make_cs()
  cs.CP, cs.radarState = control.CP, control.sm['radarState']
  controller = CanfdStopping()
  values, _ = send(cs, controller, hud=cc.hudControl)
  assert not controller.stop_object_active and values['ACC_ObjDist'] == 8.
  assert cs.radarState.leadOne.dRel == 8.
