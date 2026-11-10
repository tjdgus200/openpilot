from types import SimpleNamespace as NS

import pytest

from opendbc.car.hyundai import carcontroller, hyundaicanfd
from opendbc.car.hyundai.tests.test_steering_handover_controller import setup_controller, step
from opendbc.car.hyundai.values import CAR


def setup(monkeypatch, enabled, **kwargs):
  controller, cs, cc, settings = setup_controller(monkeypatch, **kwargs)
  controller.CP.carFingerprint = CAR.KIA_PV5
  settings['Pv5SimpleSteerRecovery'] = enabled
  params = NS(get_int=lambda key: settings.get(key, 0), get_bool=lambda key: bool(settings.get(key, False)))
  monkeypatch.setattr(carcontroller, 'Params', lambda: params)
  monkeypatch.setattr(hyundaicanfd, 'Params', lambda: params)
  return controller, cs, cc, settings


@pytest.mark.parametrize('camera', (False, True))
def test_toggle_changes_authority_only_and_reaches_real_can(monkeypatch, camera):
  traces = []
  for enabled in (False, True):
    controller, cs, cc, _ = setup(monkeypatch, enabled, camera=camera)
    trace = []
    for frame in range(360):
      cs.out.steeringPressed = frame < 100
      cs.out.steeringTorque = 350 if frame < 100 else 0
      cs.out.steeringAngleDeg = 12 if frame < 200 else -3
      cc.actuators.steeringAngleDeg = 20 if frame < 200 else -10
      output, messages = step(controller, cs, cc)
      address, byte = (0xCB, 6) if camera else (0x12A, 12)
      payload = next(data for addr, data, _ in messages if addr == address)
      assert abs(payload[byte] - output.torqueOutputCan) <= .5
      assert 25 <= output.torqueOutputCan <= 250
      trace.append((output.steeringAngleDeg, output.torqueOutputCan))
    traces.append(trace)
  assert [x[0] for x in traces[0]] == [x[0] for x in traces[1]]
  assert [x[1] for x in traces[0]] != [x[1] for x in traces[1]]


def test_simple_recovery_does_not_depend_on_valid_model_uncertainty(monkeypatch):
  traces = []
  for uncertainty in (.1, .2, .3):
    controller, cs, cc, _ = setup(monkeypatch, True)
    cs.modelV2.position.yStd = [uncertainty] * 33
    trace = []
    for frame in range(320):
      cs.out.steeringPressed = frame < 100
      cs.out.steeringTorque = 350 if frame < 100 else 0
      output, _ = step(controller, cs, cc)
      trace.append(output.torqueOutputCan)
    traces.append(trace)
  assert traces[0] == traces[1] == traces[2]
  assert traces[0][-1] == 250


@pytest.mark.parametrize('fault', ('can', 'timeout', 'temporary', 'permanent', 'frozen', 'model'))
def test_simple_recovery_retains_input_fault_admission(monkeypatch, fault):
  controller, cs, cc, _ = setup(monkeypatch, True)
  cs.out.steeringPressed = False
  cs.out.steeringTorque = 0
  for _ in range(250):
    output, _ = step(controller, cs, cc)
  assert output.torqueOutputCan == 250
  if fault == 'model':
    cs.modelV2 = None
  elif fault != 'frozen':
    setattr(cs.out, {'can': 'canValid', 'timeout': 'canTimeout', 'temporary': 'steerFaultTemporary',
                     'permanent': 'steerFaultPermanent'}[fault], fault != 'can')
  for _ in range(25):
    output, _ = step(controller, cs, cc, fresh=fault not in ('frozen', 'model'))
  assert output.torqueOutputCan == 25


def test_live_switching_is_bounded_and_deactivation_still_clears_can(monkeypatch):
  controller, cs, cc, settings = setup(monkeypatch, False)
  for _ in range(110):
    output, _ = step(controller, cs, cc)
  settings['Pv5SimpleSteerRecovery'] = True
  last = output.torqueOutputCan
  for _ in range(55):
    output, _ = step(controller, cs, cc)
    assert output.torqueOutputCan <= last + 1.875 + 1e-8
    last = output.torqueOutputCan
  assert controller.simple_steer_recovery_enabled
  settings['Pv5SimpleSteerRecovery'] = False
  for _ in range(55):
    output, _ = step(controller, cs, cc)
    assert output.torqueOutputCan <= last + 1.5 + 1e-8
    last = output.torqueOutputCan
  assert not controller.simple_steer_recovery_enabled
  cc.latActive = False
  output, messages = step(controller, cs, cc)
  assert output.torqueOutputCan == 0
  assert next(data[6] for addr, data, _ in messages if addr == 0xCB) == 0
  assert not controller.simple_steer_recovery.enabled


@pytest.mark.parametrize('angle,pv5', ((False, True), (True, False), (False, False)))
def test_other_platforms_and_torque_control_ignore_toggle(monkeypatch, angle, pv5):
  traces = []
  for enabled in (False, True):
    controller, cs, cc, settings = setup(monkeypatch, enabled, angle=angle)
    if not pv5:
      controller.CP.carFingerprint = CAR.KIA_EV6
    assert settings['Pv5SimpleSteerRecovery'] == enabled

    def forbidden(*args, **kwargs):
      pytest.fail('PV5 recovery applied outside PV5 angle steering')
    monkeypatch.setattr(controller.simple_steer_recovery, 'update', forbidden)
    trace = []
    for _ in range(120):
      output, messages = step(controller, cs, cc)
      trace.append((output.steeringAngleDeg, output.torqueOutputCan, messages))
    traces.append(trace)
    assert not controller.simple_steer_recovery_enabled
  assert traces[0] == traces[1]


@pytest.mark.parametrize('camera', (False, True))
def test_faster_simple_ramp_does_not_change_off_controller_or_can(monkeypatch, camera):
  from opendbc.car.hyundai import steering_recovery

  traces = []
  for seconds in (1.5, 1.2):
    monkeypatch.setattr(steering_recovery, 'RECOVERY_SECONDS', seconds)
    controller, cs, cc, _ = setup(monkeypatch, False, camera=camera)
    trace = []
    for frame in range(600):
      cs.out.steeringPressed = frame % 210 < 35
      cs.out.steeringTorque = 350 if cs.out.steeringPressed else 0
      cs.out.steeringAngleDeg = 12 if frame < 200 else -3
      cc.actuators.steeringAngleDeg = 20 if frame < 200 else -10
      output, messages = step(controller, cs, cc)
      trace.append((output.to_dict(), messages))
    traces.append(trace)
  assert traces[0] == traces[1]
