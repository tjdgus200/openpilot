"""Local temporary diagnostic overlay control. Does not open Bluetooth itself."""
import argparse
import secrets
import time

from openpilot.selfdrive.carrot.obd_battery import CONFIG, config_id, configuration, read_json
from openpilot.selfdrive.carrot.obd_debug import REQUEST, STATUS, MAX_SECONDS, atomic_write


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=('start', 'stop'))
  parser.add_argument('--seconds', type=int, default=600, choices=range(30, MAX_SECONDS + 1), metavar='30..600')
  args = parser.parse_args()
  if args.action == 'stop':
    REQUEST.unlink(missing_ok=True)
    print('STOP_REQUESTED: collector restores normal battery polling after the current transaction')
    return
  identity = config_id(configuration(read_json(CONFIG)))
  now = time.monotonic()
  atomic_write(REQUEST, {'version': 1, 'config_id': identity, 'nonce': secrets.token_hex(8),
                         'start': now, 'until': now + args.seconds})
  # Prior session bytes cannot appear in the new overlay.
  STATUS.unlink(missing_ok=True)
  print('REQUESTED: Park/zero/inactive-control only; six raw pages; normal battery polling temporarily paused')


if __name__ == '__main__':
  main()
