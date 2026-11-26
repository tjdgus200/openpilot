"""One advisory per indicated side, even when the CAN blinker follows the lamp phase."""
import math
import time


class ManualBsd:
  def __init__(self):
    self.enabled = False
    self.gain = 0.5
    self.warned_side = 0
    self.last_indicator = -math.inf
    self.eligible = False

  def update(self, sm, now, *, can_now_ns=None):
    self.eligible = False
    if not self.enabled or not sm.valid['carParams']:
      self.warned_side = 0
      return False
    cp = sm['carParams']
    if cp.carFingerprint != 'KIA_PV5' or cp.notCar:
      self.warned_side = 0
      return False
    for service in ('carState', 'carControl', 'selfdriveState'):
      if not sm.valid[service] or not 0 <= now - sm.recv_time[service] <= 0.3:
        return False
    cs, cc, sd = sm['carState'], sm['carControl'], sm['selfdriveState']
    if not cs.canValid:
      return False
    if cs.leftBlinker == cs.rightBlinker:
      if now - self.last_indicator > 0.8:
        self.warned_side = 0
      return False
    side = -1 if cs.leftBlinker else 1
    self.last_indicator = now
    if self.warned_side != side:
      self.warned_side = 0
    if can_now_ns is None:
      can_now_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME) if hasattr(time, 'CLOCK_BOOTTIME') else time.monotonic_ns()
    if cs.pv5OemBlindspotMonoTime == 0 or not 0 <= can_now_ns - cs.pv5OemBlindspotMonoTime <= 150_000_000:
      return False
    if not (cs.pv5OemBlindspotValid and (cs.pv5OemBlindspotLeft if side == -1 else cs.pv5OemBlindspotRight)):
      return False
    manual = not (sd.enabled or sd.active or cc.latActive or cc.longActive or cs.cruiseState.enabled)
    if cs.standstill or not cs.vEgo > 0 or cs.gearShifter != 'drive' or self.gain <= 0:
      return False
    self.eligible = manual
    first = not self.warned_side
    # Consume an overlapping assisted/other alert instead of queueing a second chime.
    self.warned_side = side
    return first and self.eligible
