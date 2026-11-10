"""PV5-only same-body SCC identity continuity through vision association loss."""
import pytest
from openpilot.selfdrive.carrot.radar_motion.controller import DPathRadarController
from openpilot.selfdrive.carrot.tests.test_radar_forced_scc_gate import Point, model, body, is_forced_scc


def test_scc_fallback_keeps_the_front_identity_of_the_same_body() -> None:
  controller = DPathRadarController(enable_radar_tracks=3, pv5_refinements=True)
  matched = controller.update(time_s=1.0, v_ego=20.0, radar_points=body(60.0, 0.3, None, v_rel=-2.0),
                              model=model(59.7, 0.0, 18.0, probability=0.95))
  assert matched.lead_one["radarTrackId"] == 38
  # Vision reads 10 m short: the front match is vetoed as farther than vision and SCC is adopted.
  fallback = controller.update(time_s=1.05, v_ego=20.0, radar_points=body(59.9, 0.3, None, v_rel=-2.0),
                               model=model(49.9, 0.0, 18.0, probability=0.95))
  lead = fallback.lead_one
  assert lead is not None and lead["radar"] and lead["radarTrackId"] == 38
  assert lead["dRel"] == pytest.approx(59.6) and lead["yRel"] == pytest.approx(0.3)
  assert lead["modelProb"] == pytest.approx(0.95)
  # SCC drops out and vision jumps nearer: switch at once, as after an anonymous SCC frame.
  dropout = controller.update(time_s=1.1, v_ego=20.0, radar_points=(Point(38, 59.5, 0.3, v_rel=-2.0),),
                              model=model(30.0, 0.0, 18.0, probability=0.95))
  assert dropout.lead_one is not None and not dropout.lead_one["radar"] and dropout.lead_one["radarTrackId"] == -1


@pytest.mark.parametrize("front",(Point(38, 57.9, 0.3, v_rel=-2.0), Point(38, 59.7, 0.3, v_rel=-3.1)))
def test_front_track_outside_the_scc_body_gate_leaves_scc_anonymous(front) -> None:
  output = DPathRadarController(enable_radar_tracks=3, pv5_refinements=True).update(
    time_s=1.0, v_ego=20.0, radar_points=(Point(0, 60.0, 0.0, v_rel=-2.0, source="scc"), front),
    model=model(50.0, 0.0, 18.0, probability=0.1),
  )
  assert is_forced_scc(output.lead_one)


