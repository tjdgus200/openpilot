"""PC-only streaming triage. Thresholds identify review candidates, not defects."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import time

from openpilot.system.pv5_incidents.archive_sanitize import excluded
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from tools.pv5_archive.schema import full_schema


def number(value):
  return float(value) if type(value) in (int, float) and math.isfinite(value) else None


class Triage:
  def __init__(self):
    self.counts, self.candidates = Counter(), {}
    self.first, self.last = None, None
    self.previous, self.last_sample = None, -1.0
    self.lead, self.scc = None, None
    self.samples, self.errors = [], set()
    self.source_commit, self.vehicle = None, None
    self.regressions, self.invalid, self.privacy_removed = 0, 0, 0
    self.event_markers, self.marker_last, self.previous_radar = [], {}, None

  def candidate(self, name, stamp):
    if name not in self.candidates and len(self.candidates) < 16:
      self.candidates[name] = {'kind': name, 'first_seconds': round(stamp - self.first, 3), 'count': 0}
    if name in self.candidates:
      self.candidates[name]['count'] = min(100000, self.candidates[name]['count'] + 1)
    relative = stamp - self.first
    important = name in ('recorded_stock_aeb', 'recorded_system_disable', 'radar_value_jump', 'new_close_target')
    if important and len(self.event_markers) < 128 and relative - self.marker_last.get(name, -100) >= 0.5:
      self.event_markers.append({'kind': name, 't': round(relative, 3)})
      self.marker_last[name] = relative

  def accept(self, event):
    topic = event.which()
    self.counts[topic] += 1
    if excluded(topic):
      self.privacy_removed += 1
      raise ValueError('received_excluded_media')
    stamp = event.logMonoTime / 1e9
    if topic == 'initData':
      commit = str(event.initData.gitCommit)
      if re.fullmatch(r'[0-9a-f]{40}', commit):
        self.source_commit = commit
      return  # Boot init timestamp is NOT the segment start.
    if topic == 'carParams':
      self.vehicle = str(event.carParams.carFingerprint)
    if topic not in ('carState', 'radarState', 'carOutput', 'selfdriveState', 'onroadEvents', 'modelV2'):
      return
    if not math.isfinite(stamp) or stamp <= 0:
      self.invalid += 1
      return
    if self.first is None:
      self.first = stamp
    self.last = max(self.last or stamp, stamp)
    if not event.valid:
      self.invalid += 1
      return
    if topic == 'radarState':
      lead = event.radarState.leadOne
      self.lead = (stamp, bool(lead.status), number(lead.dRel), number(lead.vRel))
      distance, relative = number(lead.dRel), number(lead.vRel)
      radar = (stamp, bool(lead.status), distance, relative, bool(lead.radar), int(lead.radarTrackId))
      previous = self.previous_radar
      if previous and radar[1] and previous[1] and distance is not None and relative is not None and 0 < distance < 250:
        dt = stamp - previous[0]
        if 0.01 <= dt <= 0.25 and previous[2] is not None and previous[3] is not None:
          same = radar[4] and previous[4] and radar[5] >= 0 and radar[5] == previous[5]
          moving = self.previous and 0 <= stamp - self.previous[0] <= 0.3 and self.previous[1] >= 2
          if same and (moving or relative < -1) and (
            abs(distance - (previous[2] + previous[3] * dt)) >= max(3, previous[2] * 0.25) or abs(relative - previous[3]) >= 8
          ):
            self.candidate('radar_value_jump', stamp)
          elif not same and previous[2] - distance >= 8 and distance <= 25 and relative < -1:
            self.candidate('new_close_target', stamp)
      self.previous_radar = radar
    elif topic == 'carOutput':
      actuator = event.carOutput.actuatorsOutput
      fresh = actuator.pv5SccWarningVersion == 1 and 0 <= stamp - actuator.pv5SccWarningMonoTime / 1e9 < 0.3
      self.scc = (stamp, bool(actuator.pv5SccDecelActive), number(actuator.pv5SccWarningRaw),
                  number(actuator.pv5SccWarningBaseAccel), number(actuator.accel), fresh)
    elif topic == 'onroadEvents':
      for value in event.onroadEvents:
        if value.immediateDisable or value.softDisable:
          self.candidate('recorded_system_disable', stamp)
          name = str(value.name)
          if re.fullmatch(r'[A-Za-z0-9_]{1,80}', name) and len(self.errors) < 32:
            self.errors.add(name)
    elif topic == 'carState':
      cs = event.carState
      speed, accel = number(cs.vEgo), number(cs.aEgo)
      if speed is None or accel is None or not cs.canValid or cs.canTimeout:
        self.invalid += 1
        return
      previous = self.previous
      if previous and stamp <= previous[0]:
        self.regressions += 1
        return
      jerk = None
      if previous and 0.005 <= stamp - previous[0] <= 0.25:
        jerk = (accel - previous[2]) / (stamp - previous[0])
        if previous[3] and not cs.cruiseState.enabled:
          self.candidate('cruise_release', stamp)
        if jerk <= -5 and speed >= 2:
          self.candidate('negative_jerk', stamp)
      if accel <= -3 and speed >= 2:
        self.candidate('hard_deceleration', stamp)
      if cs.stockAeb:
        self.candidate('recorded_stock_aeb', stamp)
      if stamp - self.last_sample >= 0.099:
        if len(self.samples) >= 2400:
          raise ValueError('analysis_duration_budget')
        row = {'t': round(stamp - self.first, 3), 'v': round(speed, 3), 'a': round(accel, 3), 'jerk': round(jerk, 3) if jerk is not None else None}
        if self.lead and 0 <= stamp - self.lead[0] <= 0.3 and self.lead[1]:
          distance, relative = self.lead[2:]
          if distance is not None and relative is not None and distance > 0:
            row['lead_distance'], row['lead_relative_speed'] = distance, relative
            if relative < -0.5 and distance / -relative < 2.0:
              self.candidate('short_recorded_ttc', stamp)
        if self.scc and 0 <= stamp - self.scc[0] <= 0.3:
          row['scc_active'], row['scc_raw'], row['base_accel'], row['command_accel'], row['scc_metadata_fresh'] = self.scc[1:]
          if self.scc[1]:
            self.candidate('scc_deceleration_assist', stamp)
        self.samples.append(row)
        self.last_sample = stamp
      self.previous = (stamp, speed, accel, bool(cs.cruiseState.enabled))

  def result(self):
    missing = [name for name in ('carParams', 'carState', 'radarState', 'carOutput', 'modelV2', 'onroadEvents') if not self.counts[name]]
    return {
      'version': 1,
      'status': 'review_candidates' if self.candidates else 'no_threshold_candidate',
      'source_commit': self.source_commit,
      'vehicle_pv5': self.vehicle == 'KIA_PV5',
      'duration_seconds': round(self.last - self.first, 3) if self.first is not None else None,
      'topic_counts': dict(self.counts),
      'candidates': list(self.candidates.values()),
      'error_codes': sorted(self.errors),
      'missing_services': missing,
      'invalid_messages': self.invalid,
      'time_regressions': self.regressions,
      'min_accel': min((row['a'] for row in self.samples), default=None),
      'max_speed': max((row['v'] for row in self.samples), default=None),
      'samples': self.samples,
      'event_markers': self.event_markers,
      'visual_semantics_evaluated': False,
      'conclusion': 'Threshold candidates require context; driver cancellation and legitimate hazard responses are not software defects.',
      'next_check': 'Review synchronized road recording, recorded CAN/control/lead timelines and the recorded source version. No tuning or deployment.',
    }


def analyze(path, schema, check=lambda: True):
  triage = Triage()
  wall, cpu, count = time.monotonic(), time.process_time(), 0

  def budget():
    check()
    if time.monotonic() - wall > 180 or time.process_time() - cpu > 90:
      raise ValueError('analysis_time_budget')

  with decoded(Path(path)) as stream:
    for frame in messages(stream, budget):
      count += 1
      if count > 2000000:
        raise ValueError('analysis_message_budget')
      with schema.Event.from_bytes(frame, traversal_limit_in_words=2**21) as event:
        triage.accept(event)
  return triage.result()


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--input', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  with full_schema() as schema:
    result = analyze(args.input, schema)
  args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
  main()
