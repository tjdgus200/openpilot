"""Two-stage PV5 admission reusing the existing per-frame trajectory estimates.

This does not change primary matching, CUT-IN confirmation or the legacy risk.
One bounded identity is retained, with immediate hard-veto and missing-input clear.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class AssistCandidate:
  estimate: object
  stage: int


class CutInAssistTracker:
  def __init__(self):
    self.reset()

  def reset(self):
    self.identity = None
    self.since = 0.0
    self.last_time = None

  def update(self, estimates, time_s, v_ego, primary=None, lane_change=False):
    if (
      not math.isfinite(time_s)
      or not math.isfinite(v_ego)
      or not 0.1 < v_ego <= 35.0
      or lane_change
      or (self.last_time is not None and (time_s <= self.last_time or time_s - self.last_time > 0.15))
    ):
      self.reset()
      return None
    self.last_time = time_s
    candidates = []
    for e in estimates:
      p = e.point
      numeric = (
        p.d_rel,
        p.v_rel,
        p.y_rel,
        e.d_path,
        e.future_d_path,
        e.inward_rate,
        e.reported_inward_rate,
        e.history_s,
        e.inward_progress,
        e.recent_inward_progress,
        e.direction_consistency,
        e.recent_direction_consistency,
        e.lateral_net_fraction,
        e.recent_abs_yaw_max,
        e.confidence,
      )
      if (
        p.source != "frontRadar"
        or not p.measured
        or not all(math.isfinite(v) for v in numeric)
        or not 2.0 < p.d_rel <= 30.0
        or not -8.0 <= p.v_rel <= -0.5
        or p.d_rel / -p.v_rel > 4.0
        or e.history_s < 0.5
        or e.recent_abs_yaw_max >= 0.02
        or e.unstable_fast_motion
        or e.rear_pass
        or e.parallel_drift
        or e.passing_before_overlap
        or e.entry_withdrawn
        or e.stationary_pair_alias
        or e.curve_alias
        or not e.close_front_supported
      ):
        continue
      # Stage two uses existing confirmed/control or pre-deceleration evidence;
      # it never acquires emergency authority from the weaker caution gate.
      if (e.predecel_risk or (e.confirmed_cutin and e.control_eligible)) and e.confidence >= 0.15:
        candidates.append(AssistCandidate(e, 2))
        continue
      held = self.identity == e.identity
      same_primary = primary is not None and primary.get("radar", False) and int(primary.get("radarTrackId", -1)) == p.track_id
      eta = e.time_to_overlap_s
      if (
        same_primary
        or eta is None
        or not math.isfinite(eta)
        or not 0.0 <= eta <= 1.5
        or p.d_rel + p.v_rel * eta <= 2.0
        or not 1.0 <= abs(e.d_path) <= 3.2
        or abs(e.future_d_path) >= abs(e.d_path) - 0.25
        or e.inward_progress < (0.20 if held else 0.30)
        or e.direction_consistency < 0.75
        or e.recent_direction_consistency < 0.60
        or e.recent_inward_progress < 0.05
        or e.inward_rate < 0.20
        or e.reported_inward_rate < 0.10
        or e.lateral_net_fraction < (0.30 if held else 0.60)
        or (e.jittering and not held)
      ):
        continue
      candidates.append(AssistCandidate(e, 1))
    selected = min(candidates, key=lambda c: (-c.stage, c.estimate.point.d_rel), default=None)
    if selected is None:
      self.identity = None
      self.since = time_s
      return None
    if selected.estimate.identity != self.identity:
      self.identity = selected.estimate.identity
      self.since = time_s
    # No extra dwell on strong evidence. Caution alone must persist 150ms.
    if selected.stage == 1 and time_s - self.since < 0.15:
      return None
    return selected
