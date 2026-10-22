"""Secret-free hint for keeping optional background discovery off the HUD radio."""
import json
from pathlib import Path
import time

STATUS = Path('/dev/shm/carrot-jetlink-direct-wifi.json')
INTERFACE_INDEX = Path('/sys/class/net/wljetson0/ifindex')


def direct_ap_active():
  try:
    value = json.loads(STATUS.read_text())
    age = time.monotonic() - value['updated']
    if value.get('state') != 'active' or value.get('interface') != 'wljetson0' or not age >= 0:
      return False
    if age < 8.:
      return True
    # NM/netlink queries can exceed the heartbeat's freshness window while the
    # owned AP still serves clients. Suppress optional discovery if the exact
    # interface is still present; never query NM or trigger a scan here.
    # This hint grants no packet freshness, session readiness or control validity.
    index = value.get('ifindex')
    return type(index) is int and index > 0 and int(INTERFACE_INDEX.read_text()) == index
  except (OSError, ValueError, KeyError, TypeError):
    return False
