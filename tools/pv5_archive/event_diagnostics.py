"""Bounded PC analysis of a short, authenticated incident recording."""
from fnmatch import fnmatchcase

from openpilot.system.pv5_incidents.core import HEX40, PROCESSES
from openpilot.system.pv5_incidents.priority import CRITICAL_ALERTS, REPORTABLE_ALERTS, reportable

CLIP_GLOB = 'event-' + '[0-9a-f]' * 64 + '--0'
TOPICS = frozenset({
  'carState', 'carControl', 'carOutput', 'selfdriveState', 'onroadEvents', 'managerState', 'deviceState',
  'pandaStates', 'controlsState', 'modelV2', 'roadCameraState', 'wideRoadCameraState', 'cameraOdometry',
  'livePose', 'liveCalibration', 'accelerometer', 'gyroscope', 'radarState', 'longitudinalPlan', 'parkedDashcamState',
})
SIGNALS = {
  'livePose': ('inputsOK', 'sensorsOK', 'posenetOK'),
  'carState': ('canValid', 'canTimeout'),
  'parkedDashcamState': ('inferencePaused',),
  'selfdriveState': ('parkedDashcamRequested', 'enabled', 'active'),
}


def clip_name(segment):
  return isinstance(segment, str) and fnmatchcase(segment, CLIP_GLOB)


def is_event_clip(manifest):
  return (clip_name(manifest.get('segment'))
          and [entry['name'] for entry in manifest.get('files', [])] == ['rlog.zst'])


class EventDiagnostics:
  def __init__(self, anchor_ns, schema):
    self.anchor = anchor_ns
    from openpilot.system.pv5_incidents.stationary_event_clip import DIAGNOSTICS, PRIVATE_FIELDS, private_message

    self.allowed_topics, self.private_fields = DIAGNOSTICS, PRIVATE_FIELDS
    self.private_message, self.schema = private_message, schema
    self.context = set()
    self.start = self.end = None
    self.services = {}
    self.signals = {}
    self.facts = {}

  def record_fact(self, kind, code, stamp, process=None):
    at_anchor = stamp == self.anchor
    key = kind, code, process
    if key in self.facts:
      first, anchored = self.facts[key]
      self.facts[key] = min(first, stamp), anchored or at_anchor
      return
    if len(self.facts) >= 24:
      if not at_anchor:
        return
      replace = next((key for key, (_, anchored) in self.facts.items() if not anchored), None)
      if replace is None:
        return
      del self.facts[replace]
    self.facts[key] = stamp, at_anchor

  def accept(self, event):
    topic = event.which()
    fields = self.private_fields.get(topic, ())
    if fields and any(getattr(event, topic)._has(field) for field in fields):
      raise ValueError('private_event_clip_field')
    if topic == 'initData':
      with self.schema.Event.from_bytes(self.private_message(event, self.schema)) as minimal:
        if event.initData.to_dict() != minimal.initData.to_dict():
          raise ValueError('private_event_clip_context')
    if topic in ('initData', 'carParams'):
      if (not event.valid or topic in self.context
          or (topic == 'initData' and not HEX40.fullmatch(str(event.initData.gitCommit)))
          or (topic == 'carParams' and (event.carParams.notCar or event.carParams.carVin
                                        or str(event.carParams.carFingerprint) != 'KIA_PV5'))):
        raise ValueError('invalid_event_clip_context')
      self.context.add(topic)
      return  # Minimal context may precede the clip.
    stamp = int(event.logMonoTime)
    if stamp <= 0 or topic not in self.allowed_topics:
      raise ValueError('invalid_event_clip_frame')
    self.start = stamp if self.start is None else min(self.start, stamp)
    self.end = stamp if self.end is None else max(self.end, stamp)
    if topic not in TOPICS:
      return
    evidence = self.services.setdefault(topic, [0, 0, None])
    evidence[0] += 1
    if not event.valid:
      evidence[1] += 1
      evidence[2] = stamp if evidence[2] is None else min(evidence[2], stamp)
      return
    data = getattr(event, topic)
    for name in SIGNALS.get(topic, ()):
      value = getattr(data, name)
      observed = self.signals.setdefault(topic + '.' + name, {})
      observed[value] = min(observed.get(value, stamp), stamp)
    if topic == 'onroadEvents':
      for issue in data:
        code = str(issue.name)
        if code in REPORTABLE_ALERTS and (
          issue.warning or issue.noEntry or issue.softDisable or issue.immediateDisable or issue.permanent
        ):
          kind = 'recorded_critical_alert' if code in CRITICAL_ALERTS else 'recorded_system_fault'
          self.record_fact(kind, code, stamp)
    elif topic == 'carState' and data.canValid and not data.canTimeout and data.stockAeb:
      self.record_fact('recorded_stock_aeb', 'stockAeb', stamp)
    elif topic == 'managerState':
      for item in data.processes:
        process, code = str(item.name), int(item.exitCode)
        if (process in PROCESSES and item.pid > 0 and item.shouldBeRunning and not item.running
            and reportable({'kind': 'process_exit', 'code': code})):
          self.record_fact('recorded_process_exit', code, stamp, process)

  def result(self, triage, sample_start):
    radius = 5_000_000_000
    if self.start is None or not self.anchor - radius <= self.start <= self.end <= self.anchor + radius:
      raise ValueError('invalid_event_clip_time_range')

    def seconds(stamp):
      return None if stamp is None else round((stamp - self.start) / 1e9, 6)

    facts = [{'kind': k, 'code': c, 'process': p, 't': seconds(stamp), 'at_anchor': anchored}
             for (k, c, p), (stamp, anchored) in sorted(self.facts.items(), key=lambda row: row[1][0])]
    kinds = sorted({fact['kind'] for fact in facts})
    first = facts[0]['t'] if facts else None
    duration = seconds(self.end)
    if sample_start is not None:
      offset = sample_start - self.start / 1e9
      for row in triage['samples']:
        row['t'] = round(row['t'] + offset, 6)
    anchor_recognized = any(fact['at_anchor'] for fact in facts)
    reviewable = anchor_recognized and self.context == {'initData', 'carParams'}
    triage.update(
      event_clip=True,
      duration_seconds=duration,
      event_markers=[],  # Legacy detector markers used its own clock origin.
      event_diagnostics={
        'observed_seconds': duration,
        'anchor_seconds': seconds(self.anchor),
        'anchor_recognized': anchor_recognized,
        'available_after_anchor_seconds': seconds(self.end) - seconds(self.anchor),
        'available_before_first_event_seconds': first,
        'available_after_first_event_seconds': None if first is None else round(duration - first, 6),
        'facts': facts,
        'services': {k: {'frames': c, 'invalid': bad, 'first_invalid_seconds': seconds(stamp)}
                     for k, (c, bad, stamp) in sorted(self.services.items())},
        'first_observed_flags': {k: {str(flag).lower(): seconds(stamp) for flag, stamp in values.items()}
                                 for k, values in sorted(self.signals.items())},
      },
      topic_counts={name: count for name, count in triage['topic_counts'].items()
                    if name in self.allowed_topics or name in ('initData', 'carParams')},
      status='review_candidates' if reviewable else 'insufficient_data',
      screening_policy='risk-first-v1',
      screening_uses_AI_tokens=False,
      error_codes=sorted({fact['code'] for fact in facts if isinstance(fact['code'], str)}),
      candidates=[{'kind': kind, 'first_seconds': min(f['t'] for f in facts if f['kind'] == kind),
                   'count': sum(f['kind'] == kind for f in facts)} for kind in kinds] if reviewable else [],
      review_windows=[{'start_seconds': 0., 'end_seconds': duration, 'reasons': kinds}] if reviewable else [],
      awaiting_media=False,
      next_check='Compare the recorded event time with the first invalid inputs. This clip contains telemetry only.',
    )
    return triage
