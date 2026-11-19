"""Read a bounded selected timeline, never print the whole log or private media."""

import argparse
import json
from pathlib import Path

from openpilot.system.pv5_incidents.archive_protocol import TOKEN


def selected_samples(value, index):
  windows = value.get('review_windows', [])
  if type(index) is not int or not 0 <= index < len(windows) <= 8:
    raise ValueError('invalid_selected_window')
  window = windows[index]
  rows = [row for row in value.get('samples', []) if window['start_seconds'] <= row['t'] <= window['end_seconds']]
  # At most 20 seconds of the existing 10-Hz timeline. Never silently truncate
  # a longer/differently sampled file as though it were complete evidence.
  if len(rows) > 220:
    raise ValueError('selected_timeline_budget')
  result = {'screening_policy': value['screening_policy'], 'source_commit': value.get('source_commit'),
            'selected_window': window, 'samples': rows, 'video_time_alignment_verified': False,
            'visual_semantics_evaluated': False, 'automatic_tuning': False}
  if value.get('event_clip') is True:
    result['event_diagnostics'] = value['event_diagnostics']
  return result


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--policy', type=Path, required=True)
  parser.add_argument('--digest', required=True)
  parser.add_argument('--window', type=int)
  args = parser.parse_args()
  policy = json.loads(args.policy.read_text(encoding='utf-8'))
  if not TOKEN.fullmatch(args.digest) or policy.get('enabled') is not True or policy.get('push_policy') != 'analysis_only':
    raise ValueError('invalid_review_request')
  root = Path(policy['archive']['root']) / 'analysis'
  path = root / (args.digest + ('.summary.json' if args.window is None else '.json'))
  if path.is_symlink() or path.stat().st_size > (8192 if args.window is None else 1024**2):
    raise ValueError('review_file_budget')
  value = json.loads(path.read_bytes())
  result = value if args.window is None else selected_samples(value, args.window)
  print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
  main()
