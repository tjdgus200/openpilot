"""Local web enrollment for the existing single-target, display-only BLE reader."""
import json
import os
import tempfile
import time
from pathlib import Path

from openpilot.selfdrive.carrot.obd_battery import CONFIG, SNAPSHOT, configuration, config_id, read_json

STATES = frozenset(('connecting', 'receiving', 'waiting_data', 'unavailable', 'disconnected', 'stopped',
                    'diagnostic', 'diagnostic_receiving'))


def saved(path=None):
  path = CONFIG if path is None else path
  try:
    value = read_json(path)
    configuration(value)
    return value
  except FileNotFoundError:
    return None


def status(config_path=None, snapshot_path=None, now=None):
  try:
    value = saved(config_path)
  except (OSError, ValueError, TypeError, KeyError):
    return {'registered': False, 'configError': True, 'state': 'configuration_error', 'fresh': False}
  result = {'registered': value is not None, 'configError': False, 'address': value['address'] if value else None,
            'state': 'waiting' if value else 'not_registered', 'fresh': False}
  if value is None:
    return result
  try:
    record = read_json(SNAPSHOT if snapshot_path is None else snapshot_path)
    stamp = record.get('updated')
    now = time.monotonic() if now is None else now
    if (type(stamp) not in (float, int) or not 0 <= now - stamp < 5 or
        record.get('config_id') != config_id(configuration(value))):
      return result
    state = record.get('state')
    if state in STATES:
      result['state'] = state
      result['fresh'] = state in ('receiving', 'diagnostic_receiving')
  except (OSError, ValueError, TypeError, KeyError, AttributeError):
    pass
  return result


def enroll(mac, devices, path=None):
  path = CONFIG if path is None else path
  previous = saved(path)
  if previous is not None:
    if previous['address'] != mac:
      raise ValueError('remove_registered_scanner_first')
    return  # Re-selecting the same scanner must preserve calibration.
  if not any(d.get('address') == mac for d in devices):
    raise ValueError('scan_and_select_device')
  value = {'version': 1, 'address': mac, 'confirmed_fields': []}
  configuration(value)
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(prefix='.obd-enroll-', dir=path.parent)
  try:
    with os.fdopen(fd, 'w') as stream:
      json.dump(value, stream)
      stream.flush()
      os.fsync(stream.fileno())
    # Exclusive creation: a concurrent local enrollment is never overwritten.
    os.link(temporary, path)
  finally:
    Path(temporary).unlink(missing_ok=True)


def forget(mac, path=None):
  path = CONFIG if path is None else path
  value = saved(path)
  if value is not None:
    if value['address'] != mac:
      raise ValueError('registered_scanner_changed')
    path.unlink()  # The existing manager/reader owns disconnect and teardown.
