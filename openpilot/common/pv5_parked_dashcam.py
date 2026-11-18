"""Opt-in PV5 parked recording admission and model restart checks."""
import os

FRESH_NS = 250_000_000
PARK_CONFIRM_NS = 2_000_000_000


def trial_enabled(replay=False):
  return (not replay and os.environ.get('PV5_PARKED_DASHCAM_TRIAL') == '1'
          and os.path.isfile('/AGNOS')
          and not any(os.environ.get(name) for name in ('REPLAY', 'SIMULATION', 'OPENPILOT_PREFIX', 'ZMQ')))


def enabled_for(cp, replay=False):
  return trial_enabled(replay) and cp.carFingerprint == 'KIA_PV5' and not cp.notCar and not cp.passive


def fresh(valid, stamp, now):
  return valid is True and stamp > 0 and 0 <= now - stamp < FRESH_NS


def parked(car, control):
  return (str(car.gearShifter) == 'park' and car.standstill and car.canValid and not car.canTimeout
          and car.vEgoRaw == 0 and car.vEgo == 0 and not car.gasPressed and not car.stockAeb
          and not car.cruiseState.enabled and not control.enabled and not control.latActive and not control.longActive)


def pause_allowed(sm, now, *, jetson_started, jetson_active, jetson_ready):
  return jetson_started and jetson_active and jetson_ready and recording_requested(sm, now)


def recording_requested(sm, now):
  if not all(fresh(sm.valid[k], sm.logMonoTime[k], now) for k in ('carState', 'carControl', 'selfdriveState')):
    return False
  state = sm['selfdriveState']
  return (state.parkedDashcamRequested and not state.enabled and not state.active and str(state.state) == 'disabled'
          and parked(sm['carState'], sm['carControl']))


def monitoring_pause_allowed(sm, now):
  return (recording_requested(sm, now)
          and fresh(sm.valid['parkedDashcamState'], sm.logMonoTime['parkedDashcamState'], now)
          and sm['parkedDashcamState'].inferencePaused)


def monitoring_ready(sm, now, after):
  service = 'driverMonitoringState'
  stamp = sm.logMonoTime[service]
  if stamp <= after or not fresh(sm.valid[service], stamp, now):
    return False
  dm = sm[service]
  if dm.dm2Disabled:
    # A fresh result from the user's existing disabled setting needs no frame.
    return True
  return not dm.cameraUnavailable and after < dm.cameraFrameMonoTime <= stamp


class ParkedDashcamRequest:
  """Dwell for entry only. Revoke on the first failed observation."""
  def __init__(self, start_after=0):
    self.since = None
    self.last = None
    self.requested = False
    self.resume_after = start_after

  def update(self, allowed, now):
    discontinuity = self.last is not None and not 0 <= now - self.last < FRESH_NS
    self.last = now
    if not allowed or discontinuity:
      if self.requested:
        self.resume_after = max(self.resume_after, now)
      self.since = None
      self.requested = False
    else:
      if self.since is None:
        self.since = now
      if now - self.since >= PARK_CONFIRM_NS:
        self.requested = True
    return self.requested

  def block_engagement(self, model_valid, camera_ns, plan_model_ns, downstream_ready):
    if self.requested:
      return True
    if self.resume_after:
      if not (model_valid and camera_ns > self.resume_after
              and plan_model_ns > self.resume_after and downstream_ready):
        return True
      self.resume_after = 0
    return False
