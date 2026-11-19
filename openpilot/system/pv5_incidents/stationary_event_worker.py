"""Closed event recordings only, prepared/transmitted under the live stop gate."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

from openpilot.system.pv5_incidents.archive_worker import Permission, Transport, Copier, Paused
from openpilot.system.pv5_incidents.archive_catalog import SourceChanged
from openpilot.system.pv5_incidents.archive_sanitize import PrivacyCatalog, Sanitizer
from openpilot.system.pv5_incidents.stationary_event_gate import CLIP_MODE, configuration
from openpilot.system.pv5_incidents.stationary_event_catalog import EventCatalog


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', type=Path, required=True)
  parser.add_argument('--log-root', type=Path, required=True)
  parser.add_argument('--catalog', type=Path, required=True)
  parser.add_argument('--commit', required=True)
  args = parser.parse_args()
  reason, copier = 'unqualified', None
  try:
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    cfg = configuration(args.config)
    available = os.sched_getaffinity(0) & set(range(4))
    if not available:
      raise Paused('resource')
    os.sched_setaffinity(0, available)
    resource.setrlimit(resource.RLIMIT_AS, (192 * 1024**2, 192 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    permission = Permission(sys.stdin.fileno())
    began = time.monotonic()
    while not permission.allowed() and time.monotonic() - began < 0.9:
      time.sleep(0.02)
    if not permission.allowed():
      raise Paused('state')
    from openpilot.system.pv5_incidents.runtime import read_config

    target, key = read_config()
    # Held only by this upload worker, never a logger/deleter lock.
    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    worker_lock = args.catalog.with_suffix('.lock').open('a+b')
    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    transport = Transport(target, key, permission.allowed, timeout=6)
    copier = Copier(None, transport, permission.allowed, cfg['wifi_profiles'], rate=256 * 1024)
    if cfg['qualified_modes'] == [CLIP_MODE]:
      from openpilot.system.pv5_incidents.stationary_event_clip import ClipCatalog

      copier.catalog = ClipCatalog(args.log_root, args.catalog, key, permission.allowed, copier.check)
    else:
      raw = EventCatalog(args.log_root, args.catalog, key, permission.allowed)
      raw.budget_check = copier.check
      copier.catalog = PrivacyCatalog(raw, Sanitizer(args.catalog.parent / 'privacy-cache', copier.check))
    result = copier.run(args.commit)
    reason = result['reason']
  except Paused as error:
    reason = str(error) if str(error) in ('state', 'budget', 'network', 'network_profile', 'storage', 'resource') else 'paused'
  except SourceChanged:
    reason = 'source'
  except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
    reason = 'worker_error'
  print(json.dumps({'reason': reason, 'bytes': copier.bytes if copier else 0, 'segments': copier.segments if copier else 0}), flush=True)


if __name__ == '__main__':
  main()
