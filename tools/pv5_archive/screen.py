"""Token-free PC review selection. Heuristics, never vehicle control limits."""

import math

POLICY = 'risk-first-v1'


def finite(value):
  return type(value) in (int, float) and math.isfinite(value)


def select_review(result):
  rows = result.get('samples', [])
  hits = {}

  def hit(kind, stamp):
    if finite(stamp) and stamp >= 0 and len(hits.setdefault(kind, [])) < 64:
      if not hits[kind] or stamp - hits[kind][-1] >= 0.5:
        hits[kind].append(stamp)

  previous = None
  for row in rows:
    stamp, speed, accel = (row.get(key) for key in ('t', 'v', 'a'))
    if not all(finite(value) for value in (stamp, speed, accel)):
      continue
    if speed >= 2:
      if accel <= -3:
        hit('hard_deceleration', stamp)
      # Ordinary small acceleration noise must not consume an AI review.
      if previous and 0.05 <= stamp - previous['t'] <= 0.3 and accel <= -1.5 and accel - previous['a'] <= -0.8:
        hit('abrupt_deceleration_onset', stamp)
      distance, relative = row.get('lead_distance'), row.get('lead_relative_speed')
      if finite(distance) and finite(relative) and distance > 0 and relative < -0.5:
        ttc = distance / -relative
        risk = ttc < 2 or (distance < 10 and relative <= -2)
        # Require two fresh observations for ordinary short TTC; under 1 s
        # is retained immediately. A target transition remains context, not
        # proof of radar failure or an obstacle in the driving corridor.
        if ttc < 1:
          hit('rapid_approach', stamp)
        elif risk and previous and 0.05 <= stamp - previous['t'] <= 0.3:
          old_d, old_v = previous.get('lead_distance'), previous.get('lead_relative_speed')
          if finite(old_d) and finite(old_v) and old_d > 0 and old_v < -0.5:
            if old_d / -old_v < 2 or (old_d < 10 and old_v <= -2):
              hit('rapid_approach', stamp)
    raw = row.get('scc_raw')
    if row.get('scc_metadata_fresh') is True and finite(raw) and raw <= -3:
      hit('stock_scc_hard_request', stamp)  # Requested acceleration, not measured braking.
    previous = row

  for marker in result.get('event_markers', []):
    if marker['kind'] in ('recorded_stock_aeb', 'recorded_system_disable', 'radar_value_jump', 'new_close_target'):
      hit(marker['kind'], marker['t'])
  for candidate in result.get('candidates', []):
    if candidate['kind'] in ('recorded_stock_aeb', 'recorded_system_disable', 'recorded_critical_alert', 'recorded_system_fault'):
      hit(candidate['kind'], candidate['first_seconds'])  # Retain first urgent event even if marker capacity was reached.
  # Retain normal cruise releases/assist operation as context only; they do
  # not open an AI review by themselves.
  ordered = sorted((stamp, kind) for kind, stamps in hits.items() for stamp in stamps)
  duration = result.get('duration_seconds')
  end = duration if finite(duration) and duration > 0 else max((stamp for stamp, _ in ordered), default=0) + 5
  windows = []
  for stamp, kind in ordered:
    start, stop = max(0, stamp - 5), min(end, stamp + 5)
    if windows and start <= windows[-1]['end_seconds'] and stop - windows[-1]['start_seconds'] <= 20:
      windows[-1]['end_seconds'] = round(max(stop, windows[-1]['end_seconds']), 3)
      windows[-1]['reasons'] = sorted(set(windows[-1]['reasons']) | {kind})
    else:
      windows.append({'start_seconds': round(start, 3), 'end_seconds': round(stop, 3), 'reasons': [kind]})
  priority = {kind: index for index, kind in enumerate(('recorded_stock_aeb', 'stock_scc_hard_request', 'hard_deceleration',
               'recorded_critical_alert', 'recorded_system_fault', 'recorded_system_disable', 'rapid_approach', 'radar_value_jump', 'new_close_target', 'abrupt_deceleration_onset'))}
  capped = len(windows) > 8
  if capped:
    windows = sorted(sorted(windows, key=lambda row: min(priority.get(kind, 99) for kind in row['reasons']))[:8],
                     key=lambda row: row['start_seconds'])
  result['screening_policy'] = POLICY
  result['screening_uses_AI_tokens'] = False
  result['raw_threshold_candidate_counts'] = {row['kind']: row['count'] for row in result.get('candidates', [])}
  result['candidates'] = [{'kind': kind, 'first_seconds': round(stamps[0], 3), 'count': len(stamps)} for kind, stamps in sorted(hits.items()) if stamps]
  result['review_windows'] = windows
  result['review_window_limit_reached'] = capped
  if result.get('status') != 'analysis_failed':
    result['status'] = ('review_candidates' if result['candidates'] else 'no_threshold_candidate')
  result['visual_semantics_evaluated'] = False
  result['next_check'] = 'Review only selected +/-5 s road-video/data windows; missing context stays unknown. No automatic tuning or deployment.'
  return result
