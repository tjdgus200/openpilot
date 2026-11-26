"""Read display-only wide-camera classes. Both the track and label clocks must match."""
import json
import math
import os
from pathlib import Path

# COCO identifiers in texture atlas order.
HUD_CLASSES = (0, 1, 2, 3, 5, 7)


class RadarRows:
  """Share the HUD's decoded event and project confirmed objects once per radar update."""
  def __init__(self):
    self.observe(None)

  def observe(self, event):
    self.event = event
    self.points = self.rows = self.names = None
    self.display = ()
    self.display_names = {}

  def present(self, hud):
    event = self.event
    return (event is not None and event.valid and event.logMonoTime == hud.get('mono', {}).get('liveTracks')
            and bool(hud.get('events', {}).get('liveTracks')) and len(event.liveTracks.points) > 0)

  def _points(self, hud):
    if not self.present(hud):
      return ()
    if self.points is None:
      radar = self.event.liveTracks
      self.points = ()
      if not radar.radarTrackFlipped and not any(radar.errors.to_dict().values()) and len(radar.points) <= 128:
        self.points = tuple(p for p in radar.points if p.measured and str(p.radarSource) == 'frontRadar')
    return self.points

  def get(self, hud):
    points = self._points(hud)
    if not points:
      return ()
    if self.rows is None:
      self.rows = tuple((p.trackId, p.dRel, p.yRel, p.vRel) for p in points)
    return self.rows

  def for_display(self, hud, names, speed_kph):
    if not names or not self.present(hud):
      return ()
    if self.names is names or self.names == names:
      return self.display
    from cluster_route_replay import live_track_to_radar_point
    selected = {}
    for p in self._points(hud):
      ident = p.trackId
      if ident in names:
        selected[ident] = None if ident in selected else live_track_to_radar_point(p, ident, speed_kph)
    self.names = names
    self.display = tuple(p for p in selected.values() if p is not None and p.front_track_id is not None)
    self.display_names = {p.front_track_id: names[p.front_track_id] for p in self.display}
    return self.display

HUD_RADAR = RadarRows()


def continues(row, dt, points):
  # Evaluate only already classified IDs; a competing plausible reflection vetoes the caption.
  d, y, v = row['point']
  projected = d + v * dt
  if not all(math.isfinite(x) for x in (projected, y, v)):
    return False
  found = False
  for ident, dist, lat, vel in points:
    if abs(dist - projected) <= 0.75 and abs(lat - y) <= 0.45 and abs(vel - v) <= 1.5:
      if found or ident != row['track_id']:
        return False
      found = True
  return found


class TrackClassReader:
  def __init__(self, path: Path, radar_rows=HUD_RADAR):
    self.path, self.radar_rows = path, radar_rows
    self.signature = self.packet = self.key = None
    self.poll_slot = -1
    self.expires = 0
    self.names = {}

  def _refresh(self):
    # Rust publishes by atomic rename. Read the opened generation even if another
    # replacement lands between stat() and open().
    info = self.path.stat()
    signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    if self.signature == signature:
      return
    self.packet = self.key = None
    with self.path.open('rb') as f:
      info = os.fstat(f.fileno())
      self.signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
      if info.st_size <= 32768:
        try:
          self.packet = json.loads(f.read(32769))
        except (UnicodeError, ValueError):
          pass  # Invalid publication; wait for the producer to replace it.

  def read(self, snapshot, now_ns):
    if not snapshot:
      return {}
    received, hud = snapshot
    try:
      if (not 0 <= now_ns - int(received * 1e9) <= 300_000_000 or not hud['valid']['liveTracks']
          or not hud['alive']['liveTracks']):
        return {}
      slot = now_ns // 50_000_000
      if slot != self.poll_slot:
        self.poll_slot = slot
        self._refresh()
      packet = self.packet
      if not packet or packet['version'] != 1 or packet['lens'] != 'wide':
        return {}
      stamp = hud['mono']['liveTracks']
      delta = stamp - packet['radar_ns']
      published = packet['published_monotonic_ns']
      if packet['radar_ns'] <= 0 or not 0 <= delta <= 100_000_000 or not 0 <= now_ns - published <= 300_000_000:
        return {}
      # The HUD subscriber may catch up to a new radar snapshot after the first draw.
      event = self.radar_rows.event if delta else None
      key = (stamp, id(event), bool(hud['events'].get('liveTracks')) if delta else False)
      if self.key == key and now_ns < self.expires:
        return self.names
      names = {}
      expires = published + 300_000_001
      if packet['labels']:
        points = self.radar_rows.get(hud) if delta else ()
        for row in packet['labels']:
          end = row['expires_monotonic_ns']
          if (type(row['track_id']) is int and type(row['class_id']) is int and row['class_id'] in HUD_CLASSES and 0 < end - now_ns <= 300_000_000
              and (not delta or points and continues(row, delta * 1e-9, points))):
            names[row['track_id']] = row['class_id']
            expires = min(expires, end)
      self.key, self.names, self.expires = key, names, expires
      return names
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
      self.packet = self.signature = self.key = None
      return {}
