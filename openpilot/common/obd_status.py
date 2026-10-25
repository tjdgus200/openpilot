"""Bounded, read-only UI status for the local battery collector."""
import math

from openpilot.selfdrive.carrot.obd_battery import CONFIG, SNAPSHOT, config_id, configuration, read_json


class ObdBadgeStatus:
  def __init__(self, config_path=CONFIG, snapshot_path=SNAPSHOT):
    self.config_path = config_path
    self.snapshot_path = snapshot_path
    self.next_read = 0.0
    self.received_at = None

  def update(self, now, enabled):
    if not enabled:
      self.received_at = None
      self.next_read = 0.0
      return False
    if now >= self.next_read:
      self.next_read = now + 1.0
      self.received_at = None
      try:
        identity = config_id(configuration(read_json(self.config_path)))
        record = read_json(self.snapshot_path)
        updated = record['updated']
        if (type(record.get('version')) is int and record['version'] == 1
            and record.get('config_id') == identity and record.get('state') == 'receiving'
            and type(updated) in (int, float) and math.isfinite(updated)
            and 0 <= now - updated <= 5.0):
          self.received_at = updated
      except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    # Check expiry on every UI update without re-reading either file.
    return self.received_at is not None and 0 <= now - self.received_at <= 5.0
