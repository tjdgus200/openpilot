"""Local opt-in and calibration gate for the display-only PV5 BLE reader.

Does not connect to an adapter or send diagnostic commands. No private address
is shipped in Git. Compare candidates with an independent display before confirm.
"""
import argparse
import json
import os
import time

from openpilot.selfdrive.carrot.obd_battery import (CONFIG, SNAPSHOT, FIELDS, CHARGING_PREVIEW_FIELDS,
                                                  config_id, configuration, read_json)


def save(value):
  configuration(value)
  CONFIG.parent.mkdir(parents=True, exist_ok=True)
  temporary = CONFIG.with_suffix('.tmp')
  fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, 'w') as stream:
    json.dump(value, stream, allow_nan=False)
  os.replace(temporary, CONFIG)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest='action', required=True)
  enable = sub.add_parser('enable', help='enable candidate collection; HUD fields remain blank until confirmed')
  enable.add_argument('--address', required=True)
  sub.add_parser('status', help='show fresh candidate values and confirmation state, without raw replies')
  sub.add_parser('disable', help='disable this reader; leave all other Bluetooth users unchanged')
  confirm = sub.add_parser('confirm', help='only after independent PV5 field/sign comparison')
  confirm.add_argument('--fields', required=True, nargs='+', choices=sorted(FIELDS))
  confirm.add_argument('--compared-on-pv5', action='store_true', required=True)
  args = parser.parse_args()
  os.umask(0o077)
  if args.action == 'enable':
    if CONFIG.exists():
      parser.error('configuration already exists; use status or disable before changing the target')
    save({'version': 1, 'address': args.address.upper(), 'confirmed_fields': []})
    print('ENABLED: PV5 onroad collection; charging/heatmap preview active, other fields await comparison')
  elif args.action == 'disable':
    CONFIG.unlink(missing_ok=True)
    print('DISABLED')
  elif args.action == 'confirm':
    value = read_json(CONFIG)
    configuration(value)
    value['confirmed_fields'] = sorted(set(args.fields))
    save(value)
    print('CONFIRMED: reader will reconnect using the new field selection')
  else:
    value = read_json(CONFIG)
    identity = config_id(configuration(value))
    try:
      record = read_json(SNAPSHOT)
      age = time.monotonic() - record['updated']
      fresh = record.get('config_id') == identity and 0 <= age <= 5 and record.get('state') == 'receiving'
      print(json.dumps({'state': record.get('state'), 'fresh': fresh,
                        'candidate_values_NOT_YET_CALIBRATED': record.get('candidates', {}) if fresh else {},
                        'enabled_unverified_display_fields': sorted(CHARGING_PREVIEW_FIELDS),
                        'confirmed_fields': value['confirmed_fields']}, ensure_ascii=True))
    except (OSError, ValueError, KeyError, TypeError):
      print('WAITING: no readable fresh snapshot')


if __name__ == '__main__':
  main()
