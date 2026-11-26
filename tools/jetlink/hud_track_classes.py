"""Read display-only wide-camera classes. Both the track and label clocks must match."""
import json
import math
import os
from pathlib import Path

# COCO identifiers in texture atlas order.
HUD_CLASSES = (0, 1, 2, 3, 5, 7)
HOLD_CONFIDENCE = 0.80
MAX_RADAR_GAP_NS = 250_000_000


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


def matching_point(ident, point, dt, points):
  # Evaluate only already classified IDs; a competing plausible reflection vetoes the caption.
  d, y, v = point
  projected = d + v * dt
  if not all(math.isfinite(x) for x in (projected, y, v)) or d <= 0:
    return None
  found = None
  for slot, dist, lat, vel in points:
    near = (dist > 0 and abs(dist - projected) <= 0.75 and abs(lat - y) <= 0.45 and abs(vel - v) <= 1.5)
    if slot == ident:
      if found is not None or not near:
        return None
      found = (dist, lat, vel)
    elif near:
      return None
  return found


class TrackClassReader:
  def __init__(self, path: Path, radar_rows=HUD_RADAR):
    self.path, self.radar_rows = path, radar_rows
    self.signature = self.packet = self.key = None
    self.poll_slot = -1
    self.expires = 0
    self.names = {}
    self.held = {}
    self.lost_at = {}
    self.radar_ns = self.radar_seen_ns = self.last_read_ns = 0
    self.block_publications_through = -1
    self.seed_signature = None
    self.display_fresh = self.display_names = None
    self.last_event = None

  def reset(self, now_ns=None):
    self.held.clear()
    self.lost_at.clear()
    self.radar_ns = 0
    self.block_publications_through = self.last_read_ns if now_ns is None else now_ns
    self.last_event = self.display_names = None

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

  def _fresh_names(self, snapshot, now_ns):
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
              and (not delta or points and matching_point(row['track_id'], row['point'], delta * 1e-9, points))):
            names[row['track_id']] = row['class_id']
            expires = min(expires, end)
      self.key, self.names, self.expires = key, names, expires
      return names
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
      self.packet = self.signature = self.key = None
      return {}

  def read(self, snapshot, now_ns):
    fresh = self._fresh_names(snapshot, now_ns)
    self.last_read_ns = now_ns
    if not snapshot:
      self.reset()
      return {}
    received, hud = snapshot
    try:
      source_age = hud['sent'] - hud['received']['liveTracks']
      local_age = now_ns * 1e-9 - received
      stamp = hud['mono']['liveTracks']
      if (not hud['valid']['liveTracks'] or not hud['alive']['liveTracks'] or not hud['events'].get('liveTracks')
          or stamp <= 0 or not 0 <= source_age <= .25 or not -1e-8 <= local_age <= .25
          or not source_age + local_age <= .25):
        self.reset()
        return {}
    except (KeyError, TypeError, ValueError):
      # Legacy snapshots lack the sender clock needed to extend a label.
      self.held.clear()
      self.radar_ns = 0
      return fresh
    previous = self.radar_ns
    if previous and (not 0 <= stamp - previous <= MAX_RADAR_GAP_NS or
                     not 0 <= now_ns - self.radar_seen_ns <= MAX_RADAR_GAP_NS):
      self.reset()
      return {}
    packet = self.packet
    if packet and (packet.get('version') != 1 or packet.get('lens') != 'wide'):
      self.reset()
      return {}
    if stamp != previous:
      self.radar_seen_ns = now_ns
      if self.lost_at:
        # The reader can match a camera publication at most 100 ms behind radar.
        self.lost_at = {ident: lost for ident, lost in self.lost_at.items() if stamp - lost <= 100_000_000}
    self.radar_ns = stamp
    if packet and packet.get('published_monotonic_ns', 0) <= self.block_publications_through:
      fresh = {}
    if fresh and self.lost_at:
      fresh = {ident: cls for ident, cls in fresh.items() if packet['radar_ns'] > self.lost_at.get(ident, 0)}
    # The worker publishes only visually confirmed associations. Seed once per
    # publication; radar alone may advance an existing identity but never assign one.
    seed = bool(fresh) and self.seed_signature != self.signature
    if not self.held and not seed:
      return fresh
    if (stamp == previous and self.last_event is self.radar_rows.event and not seed
        and self.display_names is not None and self.display_fresh == fresh):
      return self.display_names
    self.last_event = self.radar_rows.event
    points = self.radar_rows.get(hud)
    advanced = {}
    for ident, (class_id, point) in self.held.items():
      if fresh.get(ident, class_id) == class_id:
        current = matching_point(ident, point, (stamp - previous) * 1e-9, points)
        if current is not None:
          advanced[ident] = (class_id, current)
        else:
          self.lost_at[ident] = stamp
          if ident in fresh and packet['radar_ns'] <= stamp:
            fresh = {key: cls for key, cls in fresh.items() if key != ident}
    if seed:
      self.seed_signature = self.signature
      for row in packet['labels']:
        ident = row['track_id']
        confidence = row.get('confidence')
        point = row.get('point')
        if (fresh.get(ident) == row['class_id'] and type(confidence) in (int, float) and HOLD_CONFIDENCE <= confidence <= 1
            and isinstance(point, list) and len(point) == 3 and all(type(v) in (int, float) for v in point)
            and packet['radar_ns'] >= previous):
          current = matching_point(ident, row['point'], (stamp - packet['radar_ns']) * 1e-9, points)
          if current is not None:
            advanced[ident] = (row['class_id'], current)
    self.held = advanced
    self.display_fresh = fresh
    self.display_names = {**{ident: val[0] for ident, val in advanced.items()}, **fresh}
    return self.display_names
