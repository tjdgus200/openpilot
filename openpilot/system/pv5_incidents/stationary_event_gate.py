"""Separate opt-in, qualified stopped-vehicle event archive; never control."""
import hashlib
import json
import os
import stat
from pathlib import Path

from openpilot.system.pv5_incidents.archive_gate import Admission, Decision, finite, fresh
from openpilot.system.pv5_incidents.archive_protocol import TOKEN

CONFIG = Path('/data/pv5-pc-events.json')
MODE = 'incident_stationary_v1'
CLIP_MODE = 'incident_stationary_clip_v2'
MODULES = ('stationary_event_gate.py', 'stationary_event_catalog.py', 'stationary_event_worker.py', 'stationary_event_runtime.py', 'runtime.py', 'priority.py')


def module_identity(mode=MODE):
  from openpilot.system.pv5_incidents.archive_worker import module_identity as archive_identity
  digest = hashlib.sha256(archive_identity().encode())
  modules = MODULES + ('stationary_event_clip.py', 'core.py', 'known_sources.json') if mode == CLIP_MODE else MODULES
  for name in modules:
    digest.update(name.encode() + b'\0' + (Path(__file__).parent / name).read_bytes().replace(b'\r\n', b'\n'))
  return digest.hexdigest()


def configuration(path=CONFIG):
  info = path.lstat()
  if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_uid not in (0, os.getuid())
      or info.st_mode & 0o077 or not 0 < info.st_size <= 8192):
    raise ValueError('private_event_config_required')
  cfg = json.loads(path.read_bytes())
  if (set(cfg) != {'version', 'vehicle', 'enabled', 'qualified_modes', 'wifi_profiles', 'module_identity'}
      or type(cfg['version']) is not int or cfg['version'] not in (1, 2)
      or cfg['vehicle'] != 'KIA_PV5' or cfg['enabled'] is not True):
    raise ValueError('unqualified_event_config')
  mode = {1: MODE, 2: CLIP_MODE}[cfg['version']]
  if cfg['qualified_modes'] != [mode] or cfg['module_identity'] != module_identity(mode):
    raise ValueError('unqualified_event_config')
  profiles = cfg['wifi_profiles']
  if not isinstance(profiles, list) or not 1 <= len(profiles) <= 8 or any(not isinstance(x, str) or not TOKEN.fullmatch(x) for x in profiles):
    raise ValueError('invalid_wifi_profiles')
  return cfg


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
      return Decision(False, reason='vehicle_not_started')
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
      if cp.notCar or str(cp.carFingerprint) != 'KIA_PV5':
        return Decision(False, reason='identity')
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
      mode = CLIP_MODE if qualified_modes == [CLIP_MODE] else MODE
    if mode not in qualified_modes:
      return Decision(False, mode, 'mode_not_qualified')
    return Decision(True, mode, 'admitted')
  except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
    return Decision(False, reason='unknown')
