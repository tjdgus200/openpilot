import hashlib
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from openpilot.cereal import log
from openpilot.selfdrive.modeld.blendful import Blendful
from openpilot.selfdrive.modeld.pv5_blendful import Pv5Blendful, for_vehicle
from openpilot.selfdrive.modeld.tests.test_blendful import frame
from openpilot.selfdrive.modeld.tests.test_action_curvature import action_functions


def test_upstream_source_is_exact_except_the_constants_import_path():
  src = (Path(__file__).parents[1] / 'blendful.py').read_bytes().replace(b'\r\n', b'\n')
  original = src.replace(b'openpilot.selfdrive.modeld.constants', b'openpilot.sunnypilot.modeld_v2.constants')
  assert hashlib.sha256(original).hexdigest() == '652c52e164933a0fe585ef121e642f60e3e0a616554dcc9366b2231efefe5920'


@pytest.mark.parametrize('speed', [0., 4.9, 5., 10., 30.])
@pytest.mark.parametrize('desire,lane_change', [('none', 'off'), ('turnLeft', 'off'), ('none', 'preLaneChange')])
def test_adapter_matches_upstream_update_exactly(speed, desire, lane_change):
  owner = Pv5Blendful(NS(get_bool=lambda _: True))
  reference = Blendful()
  d, lc = getattr(log.Desire, desire), getattr(log.LaneChangeState, lane_change)
  active = desire == 'none' and lane_change == 'off'
  for i in range(160):
    output = frame(center=.3 if i < 70 else -.2, k=.001,
                   probs=(.9, .05) if 80 < i < 110 else (.9, .9), stds=(.1, .15))
    expected = reference.update(output, speed, active, .001)
    value = owner.callback(output, speed, d, lc, i*.05)(.001)
    assert value == expected and owner.valid
    assert owner.core.trust == reference.trust and owner.core.going_around == reference.going_around


def test_original_output_can_exceed_removed_acceleration_ceiling():
  owner = Pv5Blendful(NS(get_bool=lambda _: True))
  output = frame(center=.5)
  values = [owner.callback(output, 20., log.Desire.none, log.LaneChangeState.off, i*.05)(0.) for i in range(140)]
  assert values[-1]*400 > .08
  assert np.max(np.diff(values))*400/.05 <= .5+1e-9


@pytest.mark.parametrize('fingerprint,not_car', [('KIA_PV5', False), ('KIA_EV6', False), ('KIA_PV5', True), ('', False)])
def test_only_pv5_constructs_or_polls_the_adapter(fingerprint, not_car):
  reads = []
  owner = for_vehicle(NS(carFingerprint=fingerprint, notCar=not_car), NS(get_bool=lambda key: reads.append(key) or True))
  assert (owner is not None) == (fingerprint == 'KIA_PV5' and not not_car)
  if owner is not None:
    owner.callback(frame(), 20., log.Desire.none, log.LaneChangeState.off, 0.)(0.)
    assert len(reads) == 1
  else:
    assert not reads


def test_existing_live_toggle_disables_and_resets_original_state():
  enabled = [True]
  owner = Pv5Blendful(NS(get_bool=lambda _: enabled[0]))
  for i in range(100):
    owner.callback(frame(center=.4), 20., log.Desire.none, log.LaneChangeState.off, i*.05)(0.)
  assert owner.core.curvature > 0
  enabled[0] = False
  assert owner.callback({}, 20., log.Desire.none, log.LaneChangeState.off, 5.3)(0.) == 0
  assert owner.core.curvature == owner.core.trust == 0


@pytest.mark.parametrize('bad', ['missing', 'shape', 'nan', 'zero_std'])
def test_undefined_tensor_math_cannot_inject_nonfinite_correction(bad):
  output = frame(center=.4)
  if bad == 'missing':
    output.pop('lane_lines')
  elif bad == 'shape':
    output['lane_lines'] = np.zeros((1, 4, 32, 2))
  elif bad == 'nan':
    output['lane_lines'][0, 1, 10, 0] = np.nan
  else:
    output['lane_lines_stds'][0, 1:3, :, 0] = 0
  owner = Pv5Blendful(NS(get_bool=lambda _: True))
  assert owner.callback(output, 20., log.Desire.none, log.LaneChangeState.off, 0.)(0.) == 0
  assert not owner.valid and owner.core.curvature == 0


def test_action_insertion_is_before_existing_smoothing_and_longitudinal_is_unchanged():
  functions = action_functions.__wrapped__()
  output = frame(center=.4)
  output['plan'] = np.zeros((1, 33, 15))
  output['action'] = np.array([[.8, -.2]])
  previous = NS(desiredCurvature=.001, desiredAcceleration=.1, desiredVelocity=2.)
  args = (output, previous, .3, .4, 20., .2, .05)
  baseline = functions.get_action_from_model(*args)
  corrected = functions.get_action_from_model(*args, blendful=lambda k: .003)
  assert corrected.desiredCurvature == pytest.approx(functions.smooth_value(.8/400+.003, .001, .2))
  for key in ('desiredAcceleration', 'desiredVelocity', 'shouldStop'):
    assert getattr(corrected, key) == getattr(baseline, key)


def test_model_publisher_and_control_reader_do_not_apply_correction_twice():
  root = Path(__file__).parents[3]
  modeld = (root/'selfdrive/modeld/modeld.py').read_text(encoding='utf-8')
  controls = (root/'selfdrive/controls/controlsd.py').read_text(encoding='utf-8')
  assert 'lane_center.callback(model_output, v_ego, desire, DH.lane_change_state' in modeld
  assert 'lane_center.fill(modelv2_send.modelV2.pv5LaneCenter)' in modeld
  assert 'model_curvature += correction' not in controls
