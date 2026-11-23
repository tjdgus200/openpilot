"""Lane-marking evidence for manual indication and PV5 lane-change admission."""
import math
import time


PV5_LANE_MARKING_MAX_AGE_NS = 250_000_000


def fresh_pv5_lane_types(cs):
  if not cs.canValid or getattr(cs, 'canTimeout', False):
    return -1, -1
  marking = getattr(cs, 'pv5LaneMarking', None)
  if marking is None or marking.monoTime == 0:
    return -1, -1
  now = time.clock_gettime_ns(time.CLOCK_BOOTTIME) if hasattr(time, 'CLOCK_BOOTTIME') else time.monotonic_ns()
  if not 0 <= now - marking.monoTime <= PV5_LANE_MARKING_MAX_AGE_NS:
    return -1, -1
  return (marking.leftType if 0 <= marking.leftType <= 8 else -1,
          marking.rightType if 0 <= marking.rightType <= 8 else -1)


def adjacent_lane(cs, md, direction, now):
  try:
    if direction not in (1, 2):
      return False
    stamp = cs.laneLineInfoMonoTime / 1e9
    if stamp <= 0 or not 0 <= now - stamp <= .25:
      return False
    line = cs.leftLaneLine if direction == 1 else cs.rightLaneLine
    # Unknown colour is not evidence of a same-direction lane. Yellow (including
    # dashed yellow), road edge/barrier and undecided types never admit a request.
    if line not in (10, 11, 14, 15, 17):
      return False
    inner, outer, edge = (1, 0, 0) if direction == 1 else (2, 3, 1)
    if not math.isfinite(md.laneLineProbs[outer]) or md.laneLineProbs[outer] <= .5:
      return False
    std = md.roadEdgeStds[edge]
    if not math.isfinite(std) or not 0 <= std <= 1:
      return False
    sign = -1 if direction == 1 else 1
    # Require a bounded lane beyond the target boundary at the car and ahead.
    # Road-edge distance alone could describe a shoulder or an unmarked apron.
    for index in (0, 4, 8):
      a, b, e = (md.laneLines[inner].y[index], md.laneLines[outer].y[index], md.roadEdges[edge].y[index])
      if not all(math.isfinite(v) for v in (a, b, e)):
        return False
      if not 2.5 <= sign * (b - a) <= 4.8 or sign * (e - a) - std < 2.5:
        return False
    return True
  except (AttributeError, IndexError, TypeError, ValueError):
    return False
