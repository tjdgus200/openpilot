"""Vehicle admission, input resets and unchanged common vision handover."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.carrot.radar_motion.controller import DPathRadarController
from openpilot.selfdrive.carrot.tests.test_pv5_radar_stationary_shield import model, moving, stopped


@pytest.mark.parametrize("brand,fingerprint,enabled", (
  ("hyundai", "KIA_PV5", True), ("hyundai", "KIA_EV6", False),
  ("hyundai", "KIA_EV9", False), ("toyota", "TOYOTA_RAV4", False),
  ("toyota", "KIA_PV5", False), ("", "", False), ("hyundai", "", False),
))
def test_real_live_constructor_only_opts_in_the_pv5(brand, fingerprint, enabled):
  from openpilot.cereal import log
  from openpilot.selfdrive.carrot.radar import effective_radar_track_mode
  from openpilot.selfdrive.carrot.radar_motion.timing import front_radar_distance_delay_s
  path = Path(__file__).parents[1] / "radar/radard_dpath.py"
  tree = ast.parse(path.read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DPathRadarD")
  init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
  namespace = {"car": SimpleNamespace(CarParams=object), "log": log, "DPathRadarController": DPathRadarController,
                   "Params": lambda: SimpleNamespace(get_int=lambda key: 3 if key == "EnableRadarTracks" else 0),
                   "effective_radar_track_mode": effective_radar_track_mode, "corner_radar_enabled": lambda *a: False,
                   "front_radar_distance_delay_s": front_radar_distance_delay_s, "PRODUCTION_CUT_IN_SENSITIVITY": 3}
  exec(compile(ast.Module(body=[init], type_ignores=[]), str(path), "exec"), namespace)
  instance = SimpleNamespace()
  namespace["__init__"](instance, SimpleNamespace(brand=brand, carFingerprint=fingerprint,
                                                 radarUnavailable=False, radarDelay=0., flags=0))
  assert instance.controller.pv5_refinements is enabled
  assert instance.controller.primary_matcher.enable_fresh_stationary_shield is enabled


@pytest.mark.parametrize("metadata,enabled", (
  ((('hyundai', 'KIA_PV5'),), True), ((('hyundai', 'KIA_EV6'),), False),
  ((('hyundai', 'KIA_PV5'), ('hyundai', 'KIA_EV6')), False),
  ((('hyundai', 'KIA_PV5'), ('', '')), False), ((), False),
))
def test_real_replay_constructor_uses_complete_consistent_metadata(monkeypatch, metadata, enabled):
  from openpilot.selfdrive.carrot.radar.tools import radar_validation_replay as replay
  from openpilot.selfdrive.carrot.tests.test_radar_lead_simulator import frame
  from dataclasses import replace
  frames = [replace(frame((), time_s=i * .05), car_brand=b, car_fingerprint=f) for i, (b, f) in enumerate(metadata)]
  actual = []
  def controller(**kwargs):
    actual.append(kwargs["pv5_refinements"])
    return DPathRadarController(**kwargs)
  monkeypatch.setattr(replay, "DPathRadarController", controller)
  replay._production_controller_outputs(frames, motion_sensor="front", enable_radar_tracks=3, cut_in_sensitivity=0)
  assert actual == [enabled]


@pytest.mark.parametrize("interruption", ("reset", "missing_path", "reverse", "gap", "invalid"))
def test_refinement_history_does_not_survive_a_new_input_sequence(interruption):
  controller = DPathRadarController(enable_radar_tracks=3, pv5_refinements=True)
  points = (moving(38, 40., 22.3), stopped(42, 20.))
  for i in range(10):
    controller.update(i * .05, 20.8, points, model(40., 22.3))
  controller.update(.5, 20.8, points, model(19., 0.))
  matcher = controller.primary_matcher
  assert matcher._vision_continuous_frames == 0
  if interruption == "reset":
    matcher.reset()
    assert matcher._moving_selected is matcher._vision_previous is matcher._match_time_s is None
  elif interruption == "missing_path":
    controller.update(.55, 20.8, points, SimpleNamespace())
    assert matcher._moving_selected is matcher._vision_previous is matcher._match_time_s is None
  else:
    stamp = {"reverse": .4, "gap": 1., "invalid": None}[interruption]
    matcher.match(model(19., 0.), (), ((0., 0.), (100., 0.)), time_s=stamp)
    assert matcher._vision_continuous_frames in (0, 3)
    assert matcher._moving_selected is None


def test_default_is_original_policy_without_refinement_work():
  controller = DPathRadarController(enable_radar_tracks=3)
  for i in range(10):
    controller.update(i * .05, 20.8, (moving(38, 40., 22.3),), model(40., 22.3))
  assert not controller.pv5_refinements
  assert controller.primary_matcher._match_time_s is None
