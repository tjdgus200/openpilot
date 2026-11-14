"""Default-off PV5 cut-in constraints; values are PC trial calibrations."""

from dataclasses import dataclass
import math

from opendbc.car.hyundai.scc_decel import eligible as scc_eligible

MAX_AGE_NS = 150_000_000


def eligible(cp):
  # Missing configuration must decline the new experiment rather than crash
  # an existing radar-only/partially constructed consumer.
  return bool(cp is not None and getattr(cp, 'openpilotLongitudinalControl', False) and not getattr(cp, 'notCar', True) and scc_eligible(cp))


@dataclass(frozen=True)
class CutInRequest:
  stage: int = 0
  limit: float = 0.0
  stamp: int = 0


def fresh(stamp, now):
  return isinstance(stamp, int) and 0 < stamp <= now and now - stamp <= MAX_AGE_NS


def request_from_radar(cp, radar, now):
  if not eligible(cp) or radar is None:
    return CutInRequest()
  stage = int(radar.pv5CutInAssistStage)
  lead = radar.leadCutInAssist
  if stage not in (1, 2) or not lead.status or not lead.radar or not fresh(radar.mdMonoTime, now) or not fresh(radar.carStateMonoTime, now):
    return CutInRequest()
  values = (lead.dRel, lead.vRel, lead.score)
  if not all(math.isfinite(v) for v in values) or not 2.0 < lead.dRel <= 30.0 or not -8.0 <= lead.vRel <= -0.5 or not 0.15 <= lead.score <= 1.0:
    return CutInRequest()
  # Same TTC-dependent ceiling as the existing cutin_predecel request. The
  # legacy function is unchanged; parity is checked over its valid range.
  ttc = min(8.0, max(2.5, lead.dRel / max(-lead.vRel, 0.1)))
  limit = 0.0 if stage == 1 else -0.65 + (ttc - 2.5) / 5.5 * 0.40
  return CutInRequest(stage, limit, int(radar.mdMonoTime))


def constrain_accel(accel, request):
  return min(accel, request.limit) if request.stage else accel


def feedback_stage(cp, output, output_stamp, now):
  stage = int(output.pv5CutInAssistStage)
  return (
    stage
    if (
      eligible(cp)
      and output.pv5CutInAssistEnabled
      and stage in (1, 2)
      and fresh(output_stamp, now)
      and fresh(output.pv5CutInAssistSourceMonoTime, now)
      and math.isfinite(output.accel)
      and -4.0 <= output.accel <= 0.0
    )
    else 0
  )


def warning_stage(cp, cs, output, output_stamp, now):
  if not cs.canValid or cs.canTimeout or cs.brakePressed or cs.gasPressed or cs.parkingBrake or cs.brakeHoldActive or str(cs.gearShifter) != "drive":
    return 0
  return feedback_stage(cp, output, output_stamp, now)
