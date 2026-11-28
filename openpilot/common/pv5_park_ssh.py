"""Local preference and public readiness of the qualified Park SSH guard."""

import os
from pathlib import Path
import stat

PARAM = 'PV5ParkAutoSsh'
PREFERENCE = Path('/data/params/d') / PARAM
MARKER_NAME = 'park-auto-qualified'
MARKER = Path('/run/pv5-conditional-remote') / MARKER_NAME
PARK_MARKER = Path('/run/pv5-park-remote') / MARKER_NAME


class QualificationRequired(ValueError):
  """The root-owned Park guard must be qualified before enabling the setting."""


def requested(path=PREFERENCE):
  # The guard runs as root. An unexpected param entry must neither block the
  # watchdog nor make it follow a symlink to another file.
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
      return stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, 2) == b'1'
    finally:
      os.close(fd)
  except (AttributeError, OSError):
    return False


def qualified(marker=None):
  if marker is None:
    return qualified(MARKER) or qualified(PARK_MARKER)
  try:
    root = marker.parent.lstat()
    entry = marker.lstat()
    return (stat.S_ISDIR(root.st_mode) and stat.S_ISREG(entry.st_mode)
            and root.st_uid == entry.st_uid == 0
            and not (root.st_mode | entry.st_mode) & 0o022 and entry.st_size == 0)
  except OSError:
    return False
