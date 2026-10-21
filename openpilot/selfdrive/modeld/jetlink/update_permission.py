"""Permit setup offroad or from fresh Park and zero-speed vehicle state.

This does not change ignition, vehicle messages, control flags or model joining.
The USB boot gate has no active inference engine; live runtimes are not updated.
"""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import time

SERVICES = ('carState',)


def parked_stationary(sm, now):
  try:
    if not all(sm.valid[k] and sm.alive[k] and 0 <= now - sm.recv_time[k] < .25 for k in SERVICES):
      return False
    cs = sm['carState']
    # Use the CAN wheel-speed value, not a Kalman tail after the vehicle stops.
    # Missing raw speed is unavailable; it must not become a default zero.
    speed = cs.vEgoRaw
    return (cs.canValid and not cs.canTimeout and type(speed) in (int, float)
            and math.isfinite(speed) and speed == 0.0 and str(cs.gearShifter) == 'park')
  except (AttributeError, KeyError, TypeError, ValueError):
    return False


class Permission:
  def __init__(self, params, factory=None, clock=time.monotonic, allow_parked=True):
    self.params, self.factory, self.clock = params, factory, clock
    self.sm = None
    self.next_sample = 0.
    self.cached = False
    self.allow_parked = allow_parked

  def allowed(self):
    try:
      offroad, onroad = self.params.get_bool('IsOffroad'), self.params.get_bool('IsOnroad')
      if offroad and not onroad:
        return True
      if offroad or not onroad or not self.allow_parked:
        return False
      now = self.clock()
      if now >= self.next_sample:
        if self.sm is None:
          if self.factory is None:
            from openpilot.cereal import messaging
            self.sm = messaging.SubMaster(list(SERVICES))
          else:
            self.sm = self.factory()
        self.sm.update(0)
        now = self.clock()
        self.cached = parked_stationary(self.sm, now)
        self.next_sample = now + .05
      # Age checks are repeated even between 20Hz subscriber polls.
      return self.cached and parked_stationary(self.sm, self.clock())
    except Exception:
      self.cached = False
      return False


def wait_for_permission(permission, connected, waiting, sleep=time.sleep):
  # New subscribers need actual messages before they can approve anything.
  # Retain the session instead of repeatedly recreating an empty subscriber.
  while connected():
    if permission.allowed():
      return
    waiting()
    sleep(.2)
  raise ConnectionError('USB setup requires offroad or fresh Park/zero-speed state and USB connection')


@contextmanager
def bulk_scheduling():
  """No real-time/core7 bulk work; leave existing live-inference policy alone."""
  if not Path('/TICI').is_file():
    yield
    return
  policy, priority = os.sched_getscheduler(0), os.sched_getparam(0)
  affinity = os.sched_getaffinity(0)
  try:
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    os.sched_setaffinity(0, set(range(min(4, os.cpu_count() or 1))))
    yield
  finally:
    os.sched_setaffinity(0, affinity)
    os.sched_setscheduler(0, policy, priority)
