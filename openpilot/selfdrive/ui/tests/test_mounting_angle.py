"""Direction, validity and real-HUD admission for the PV5 mounting readout."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.ui.onroad import mounting_angle as mounting

pytest_plugins = ['openpilot.selfdrive.ui.tests.test_carrot_hud_renderer']


def rpy(pitch, yaw):
  return (0., math.radians(pitch), math.radians(yaw))


@pytest.mark.parametrize('pitch,yaw,expected', [
  (-3, 2, '^ 3° | < 2°'), (3, -2, 'v 3° | > 2°'),
  (-3, -2, '^ 3° | > 2°'), (3, 2, 'v 3° | < 2°'),
  (0, 0, '0° | 0°'), (-0.01, 0.01, '0° | 0°'),
  (-3.26, -2.16, '^ 3.3° | > 2.2°'),
])
def test_owner_examples_and_device_settings_direction_convention(pitch, yaw, expected):
  # Stock DeviceLayout maps positive pitch to down and positive yaw to left.
  assert mounting.format_mounting_angles(rpy(pitch, yaw)) == expected


@pytest.mark.parametrize('value', [None, (), (0, 0), (0, 0, 0, 0),
  (0, math.nan, 0), (0, 0, math.inf), (math.inf, 0, 0), (0, 4, 0), (0, 'bad', 0)])
def test_malformed_orientation_is_unknown(value):
  assert mounting.format_mounting_angles(value) is None


class SM(dict):
  def __init__(self):
    super().__init__(liveCalibration=NS(rpyCalib=rpy(-3, 2), calStatus='calibrated'), selfdriveState=NS(alertSize=0))
    self.recv_frame = dict.fromkeys(self, 20)
    self.seen = dict.fromkeys(self, True)
    self.alive = dict.fromkeys(self, True)
    self.valid = dict.fromkeys(self, True)


def state():
  return NS(CP=NS(brand='hyundai', carFingerprint='KIA_PV5', notCar=False),
            sm=SM(), started_frame=10)


def test_uses_existing_subscriber_and_reformats_only_new_source_frame(monkeypatch):
  ui = state()
  ui.params = NS(get=lambda *a: pytest.fail('no persistent parameter reads'))
  display = mounting.MountingAngleDisplay()
  original = mounting.format_mounting_angles
  calls = []
  monkeypatch.setattr(mounting, 'format_mounting_angles', lambda values: (calls.append(1), original(values))[1])
  display.update(ui)
  for _ in range(30):
    display.update(ui)
  assert display.visible and display.valid and display.value == '^ 3° | < 2°'
  assert len(calls) == 1
  ui.sm['liveCalibration'].rpyCalib = rpy(3, -2)
  ui.sm.recv_frame['liveCalibration'] += 1
  display.update(ui)
  assert display.value == 'v 3° | > 2°' and len(calls) == 2


@pytest.mark.parametrize('change', ['unseen', 'dead', 'invalid', 'previous_start',
                                   'uncalibrated', 'bad_calibration', 'recalibrating', 'bad_vector', 'missing_message'])
def test_source_loss_clears_previously_valid_estimate(change):
  ui = state()
  display = mounting.MountingAngleDisplay()
  display.update(ui)
  assert display.valid
  if change in ('unseen', 'dead', 'invalid'):
    getattr(ui.sm, {'unseen':'seen', 'dead':'alive', 'invalid':'valid'}[change])['liveCalibration'] = False
  elif change == 'previous_start':
    ui.started_frame = 21
  elif change == 'missing_message':
    del ui.sm['liveCalibration']
    ui.sm.recv_frame['liveCalibration'] += 1
  else:
    ui.sm.recv_frame['liveCalibration'] += 1
    if change == 'bad_vector':
      ui.sm['liveCalibration'].rpyCalib = (0, math.nan, 0)
    else:
      ui.sm['liveCalibration'].calStatus = {'bad_calibration':'invalid'}.get(change, change)
  display.update(ui)
  assert display.visible and not display.valid and display.value == '-- | --'


@pytest.mark.parametrize('change', ['other_brand', 'other_car', 'not_car', 'unknown_cp'])
def test_other_profiles_have_no_readout(change):
  ui = state()
  display = mounting.MountingAngleDisplay()
  display.update(ui)
  if change == 'other_brand':
    ui.CP.brand = 'other'
  elif change == 'other_car':
    ui.CP.carFingerprint = 'OTHER'
  elif change == 'not_car':
    ui.CP.notCar = True
  else:
    ui.CP = None
  display.update(ui)
  assert not display.visible and not display.valid


@pytest.mark.parametrize('size', [1, 2, 3])
def test_all_existing_alert_sizes_preempt_and_restore_the_readout(size):
  ui = state()
  display = mounting.MountingAngleDisplay()
  display.update(ui)
  assert not display.suppressed
  ui.sm['selfdriveState'].alertSize = size
  display.update(ui)
  assert display.suppressed and display.value == '^ 3° | < 2°'
  ui.sm['selfdriveState'].alertSize = 0
  display.update(ui)
  assert not display.suppressed


@pytest.mark.parametrize('field', ['seen', 'alive', 'valid'])
def test_missing_warning_state_preempts_information(field):
  ui = state()
  display = mounting.MountingAngleDisplay()
  display.update(ui)
  getattr(ui.sm, field)['selfdriveState'] = False
  display.update(ui)
  assert display.suppressed


def test_real_hud_draws_user_format_without_changing_other_vehicle_pixels(hud_module, monkeypatch):
  module, ui = hud_module
  ui.CP = state().CP
  ui.sm, ui.started_frame = SM(), 10
  renderer = module.HudRenderer()
  renderer._mounting_angle.update(ui)
  drawn = []
  monkeypatch.setattr(module.rl, 'draw_text_ex', lambda *args: drawn.append(args))
  renderer._draw_mounting_angle(module.rl.Rectangle(30, 30, 2100, 1020))
  assert drawn[0][1] == 'Mounting angle: ^ 3° | < 2°'
  ui.CP.carFingerprint = 'OTHER'
  renderer._mounting_angle.update(ui)
  drawn.clear()
  renderer._draw_mounting_angle(module.rl.Rectangle(30, 30, 2100, 1020))
  assert drawn == []


def test_hud_update_path_uses_live_calibration(hud_module):
  module, ui = hud_module
  ui.CP, ui.sm, ui.started_frame = state().CP, SM(), 10
  ui.sm['carState'] = NS(vCruiseCluster=85., vEgoCluster=0., vEgo=20.)
  ui.sm['controlsState'] = NS(deprecated=NS(vCruise=85.))
  ui.sm['selfdriveState'].enabled = True
  ui.sm.recv_frame.update(carState=20, deviceState=1, peripheralState=1)
  renderer = module.HudRenderer()
  renderer._device_info_loaded = True
  renderer._device_info_recv_frames = (1, 1)
  renderer._update_state()
  assert renderer._mounting_angle.visible and not renderer._mounting_angle.suppressed
  assert renderer._mounting_angle.value == '^ 3° | < 2°'
