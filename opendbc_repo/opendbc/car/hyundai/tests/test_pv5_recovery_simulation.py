"""Offline reduced-order feedback simulations; NOT a calibrated PV5/EPS model.

Use the actual controller, limits and CAN packer, with synthetic lane planning,
driver effort and a bounded second-order steering plant. No extra vehicle worker.
An explicit PV5_RECOVERY_SIM_REPORT path optionally stores reproducible PC results.
"""
from collections import deque
import hashlib
import itertools
import json
import math
import os
from pathlib import Path

import pytest

from opendbc.car.hyundai.tests.test_pv5_steering_recovery import setup
from opendbc.car.hyundai.tests.test_steering_handover_controller import step
from opendbc.car.hyundai import carcontroller, steering_recovery


CASES = list(itertools.product((10., 15., 22.), (180., 360.), (0., .08), (1., 1.5), (-1, 1)))
RESULTS = []


def simulate(monkeypatch, adaptive, *, speed, eps_accel, delay, tightening, direction):
  controller, cs, cc, settings = setup(monkeypatch, True)
  controller.CP.brand = 'hyundai'
  settings.update(Pv5SteerReleaseMs=200, Pv5SteerRecoveryMs=1500)
  if not adaptive:
    monkeypatch.setattr(controller.simple_steer_recovery, '_update_boost', lambda *args: None)
  dt, wheelbase, ratio = .01, controller.CP.wheelbase, controller.CP.steerRatio
  road_start = direction * 1.2 / speed**2
  steering = math.degrees(math.atan(road_start * wheelbase)) * ratio
  cs.out.vEgoRaw = cs.out.vEgo = speed
  cs.out.steeringAngleDeg = cc.actuators.steeringAngleDeg = steering
  cs.out.steeringPressed, cs.out.steeringTorque = False, 0
  for _ in range(350):
    output, _ = step(controller, cs, cc)
  assert output.torqueOutputCan == 250
  cs.out.steeringPressed, cs.out.steeringTorque = True, direction * 350
  samples = []
  for tick in range(80):
    previous = output.torqueOutputCan
    output, _ = step(controller, cs, cc)
    assert output.torqueOutputCan <= previous
    if tick % 5 == 4:
      samples.append({'time': (tick + 1) * dt - .8, 'lateral_m': 0., 'steering_deg': steering,
                      'desired_deg': cc.actuators.steeringAngleDeg, 'command_deg': output.steeringAngleDeg,
                      'cap': output.torqueOutputCan, 'boost': 0., 'released': False, 'driver_pressed': True})
  cs.out.steeringPressed, cs.out.steeringTorque = False, 0
  queue = deque([(output.steeringAngleDeg, output.torqueOutputCan)] * round(delay / dt))
  lateral = heading = steering_rate = integral = peak = 0.
  peak_angle_error = peak_boost = peak_cap_step = 0.
  previous_cap = output.torqueOutputCan
  for tick in range(500):
    elapsed = tick * dt
    road = road_start * (1 + (tightening - 1) * min(1., elapsed / .6))
    if tick % 5 == 0:
      # Synthetic 20Hz planner with feedforward curvature and lateral/heading feedback.
      desired_curvature = road - (2 * lateral + 2.5 * speed * heading) / speed**2
      cc.actuators.steeringAngleDeg = math.degrees(math.atan(wheelbase * desired_curvature)) * ratio
    cs.out.steeringAngleDeg = steering
    output, messages = step(controller, cs, cc)
    cap = output.torqueOutputCan
    assert all(math.isfinite(v) for v in (cap, output.steeringAngleDeg, steering, lateral, heading))
    assert 25 <= cap <= 250
    assert 0 <= cap - previous_cap <= 2.25 + 1e-8
    assert abs(output.steeringAngleDeg) <= controller.params.ANGLE_LIMITS.STEER_ANGLE_MAX
    assert abs(next(data[6] for addr, data, bus in messages if addr == 0xCB) - cap) <= .5
    peak_cap_step = max(peak_cap_step, cap - previous_cap)
    previous_cap = cap
    peak_boost = max(peak_boost, controller.simple_steer_recovery.recovery_boost)
    queue.append((output.steeringAngleDeg, cap))
    command, applied_cap = queue.popleft()
    # Assumed EPS inertia/damping/self-aligning load, not measured CAN-to-torque calibration.
    demand = 60 * (command - steering) - 14 * steering_rate
    limit = eps_accel * applied_cap / 250
    acceleration = max(-limit, min(limit, demand)) - 2 * steering
    steering_rate += acceleration * dt
    steering += steering_rate * dt
    actual_curvature = math.tan(math.radians(steering / ratio)) / wheelbase
    heading += speed * (actual_curvature - road) * dt
    lateral += speed * math.sin(heading) * dt
    integral += abs(lateral) * dt
    peak = max(peak, abs(lateral))
    peak_angle_error = max(peak_angle_error, abs(command - steering))
    if tick % 5 == 4:
      samples.append({'time': (tick + 1) * dt, 'lateral_m': lateral, 'steering_deg': steering,
                      'desired_deg': cc.actuators.steeringAngleDeg, 'command_deg': output.steeringAngleDeg,
                      'cap': cap, 'boost': controller.simple_steer_recovery.recovery_boost,
                      'released': controller.simple_steer_recovery.released, 'driver_pressed': False})
  return {'peak_lateral_m': peak, 'integral_abs_lateral_m_s': integral,
          'peak_angle_error_deg': peak_angle_error, 'peak_boost': peak_boost,
          'peak_cap_step': peak_cap_step, 'samples': samples}


@pytest.mark.parametrize('speed,eps_accel,delay,tightening,direction', CASES)
def test_synthetic_curve_handover(monkeypatch, speed, eps_accel, delay, tightening, direction):
  arguments = {'speed': speed, 'eps_accel': eps_accel, 'delay': delay, 'tightening': tightening, 'direction': direction}
  fixed = simulate(monkeypatch, False, **arguments)
  adaptive = simulate(monkeypatch, True, **arguments)
  RESULTS.append(arguments | {'fixed': fixed, 'adaptive': adaptive})
  # Guard the comparative behavior of this parameter grid, not physical road safety.
  assert adaptive['peak_cap_step'] <= 2.25 + 1e-8
  assert adaptive['peak_lateral_m'] <= fixed['peak_lateral_m'] + 1e-9


def test_optional_pc_report():
  path = os.environ.get('PV5_RECOVERY_SIM_REPORT')
  if path:
    assert len(RESULTS) == len(CASES)
    report = {'calibrated_pv5_or_eps': False, 'real_neural_model_executed': False,
              'scope': 'Actual controller/CAN; assumed bicycle/planner/EPS parameters; no road-safety claim.',
              'settings_ms': {'confirmation': 200, 'base_recovery': 1500},
              'source_hashes': {name: hashlib.sha256(Path(source).read_bytes()).hexdigest()
                                for name, source in (('controller', carcontroller.__file__),
                                                     ('recovery', steering_recovery.__file__),
                                                     ('simulation', __file__))},
              'cases': RESULTS}
    Path(path).write_text(json.dumps(report, indent=2), encoding='utf8')
