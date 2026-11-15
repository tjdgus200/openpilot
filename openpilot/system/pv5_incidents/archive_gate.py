"""Auxiliary upload admission. Never publishes CAN, controls, or model input."""

from dataclasses import dataclass
import math

SERVICES = ('carParams', 'carState', 'onroadEvents', 'carControl', 'selfdriveState', 'deviceState', 'pandaStates', 'managerState', 'roadEncodeIdx')


def finite(value):
  return type(value) in (int, float) and math.isfinite(value)


def fresh(sm, name, now, ttl):
  try:
    stamp = sm.logMonoTime[name] / 1e9
    return sm.seen[name] and sm.valid[name] and finite(stamp) and 0 <= now - stamp < ttl
  except (AttributeError, KeyError, TypeError, ValueError):
    return False


@dataclass(frozen=True)
class Decision:
  allowed: bool
  mode: str = ''
  reason: str = 'unknown'


def evaluate(sm, now, qualified_modes, owner_pv5):
  try:
    if owner_pv5 is not True or not finite(now):
      return Decision(False, reason='identity')
    if sm.seen.get('carParams', False) and sm.valid.get('carParams', False):
      cp = sm['carParams']
      if cp.notCar or str(cp.carFingerprint) != 'KIA_PV5':
        return Decision(False, reason='identity')
    if not all(fresh(sm, name, now, ttl) for name, ttl in (('deviceState', 1.5), ('pandaStates', 0.25), ('managerState', 1.5))):
      return Decision(False, reason='state_stale')
    ds, ps, ms = sm['deviceState'], sm['pandaStates'], sm['managerState']
    if int(ds.thermalStatus.raw) != 0:
      return Decision(False, reason='resource')
    if str(ds.networkType) != 'wifi' or ds.networkMetered is not False:
      return Decision(False, reason='wifi_required')
    if len(ps) > 8 or len(ms.processes) > 128:
      return Decision(False, reason='state_size')
    processes = {str(value.name): (value.running, value.shouldBeRunning) for value in ms.processes}
    if len(processes) != len(ms.processes) or any(processes.get(name) != (True, True) for name in ('pandad', 'hardwared')):
      return Decision(False, reason='producer')
    if ds.started is False:
      controls = ('card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd')
      if any(processes.get(name) != (False, False) for name in controls):
        return Decision(False, reason='process_active')
      if not all(
        str(board.pandaType) != 'unknown' and str(board.harnessStatus) == 'notConnected' and not board.ignitionLine and not board.ignitionCan for board in ps
      ):
        return Decision(False, reason='not_detached')
      mode = 'detached_offroad'
    else:
      usage = list(ds.cpuUsagePercent)
      if (not usage or len(usage) > 32 or not all(finite(value) and 0 <= value <= 70 for value in usage)
          or not 0 <= ds.memoryUsagePercent <= 80 or not 20 <= ds.freeSpacePercent <= 100):
        return Decision(False, reason='resource')
      # This separate mode remains disabled until THAT mode is measured on device.
      if not all(fresh(sm, name, now, 0.25) for name in ('carState', 'carControl', 'selfdriveState')):
        return Decision(False, reason='control_stale')
      if not sm.seen['carParams'] or not sm.valid['carParams'] or not fresh(sm, 'onroadEvents', now, 2.5):
        return Decision(False, reason='profile_or_events')
      cp, cs, cc, sd = sm['carParams'], sm['carState'], sm['carControl'], sm['selfdriveState']
      if cp.notCar or str(cp.carFingerprint) != 'KIA_PV5' or not (cp.passive or cp.dashcamOnly):
        return Decision(False, reason='dashcam_required')
      if (
        cs.canValid is not True
        or cs.canTimeout is not False
        or cs.standstill is not True
        or not finite(cs.vEgoRaw)
        or cs.vEgoRaw != 0
        or not finite(cs.vEgo)
        or abs(cs.vEgo) > 0.01
      ):
        return Decision(False, reason='not_stationary')
      if (
        cs.cruiseState.enabled
        or cc.enabled
        or cc.latActive
        or cc.longActive
        or sd.enabled
        or sd.active
        or str(sd.state) != 'disabled'
        or str(sd.alertStatus) == 'critical'
        or cs.stockAeb
      ):
        return Decision(False, reason='control_or_emergency')
      if len(sm['onroadEvents']) > 64 or any(value.immediateDisable or value.softDisable for value in sm['onroadEvents']):
        return Decision(False, reason='emergency')
      mode = 'dashcam_stationary'
    if mode not in qualified_modes:
      return Decision(False, mode, 'mode_not_qualified')
    return Decision(True, mode, 'admitted')
  except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
    return Decision(False, reason='unknown')


class Admission:
  def __init__(self):
    self.good_since = None
    self.last = None
    self.mode = ''

  def tick(self, decision, now):
    if self.last is not None and (not finite(now) or now < self.last or now - self.last >= 0.75):
      self.good_since = None
    self.last = now
    if not decision.allowed or decision.mode != self.mode:
      self.good_since = None
    self.mode = decision.mode
    if not decision.allowed:
      return False
    if self.good_since is None:
      self.good_since = now
    return now - self.good_since >= 2.0
