"""Small, display-only validation of optional Jetson quality observations."""
import math

STATES = ('waiting', 'warmup', 'measuring', 'ready', 'fallback')
FIELDS = ('mean_ms', 'p99_ms', 'max_ms', 'server_p99_ms')


def finite(value, low, high):
  return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def quality_status(value):
  if not isinstance(value, dict) or value.get('version') != 1:
    return None
  kind = value.get('kind', 'codec')
  if kind not in ('codec', 'fps-q92'):
    return None
  limit = 5 if kind == 'fps-q92' else 8
  if any(type(value.get(k)) is not int or not 0 <= value[k] < limit for k in ('active', 'effective')):
    return None
  if value.get('state') not in STATES or not isinstance(value.get('rows'), list) or len(value['rows']) > limit:
    return None
  rows, ids = [], set()
  for row in value['rows']:
    if (not isinstance(row, dict) or type(row.get('id')) is not int or not 0 <= row['id'] < limit
        or row['id'] in ids or type(row.get('n')) is not int or not 1 <= row['n'] <= 6000
        or not finite(row.get('seconds'), 0, 3600)
        or not all(finite(row.get(k), 0, 10000) for k in FIELDS)
        or not all(type(row.get(k)) is int and 0 <= row[k] <= row['n'] for k in ('over50', 'server_over50'))
        or row['max_ms'] < max(row['mean_ms'], row['p99_ms'])):
      return None
    ids.add(row['id'])
    rows.append({k: row[k] for k in ('id', 'n', 'seconds', *FIELDS, 'over50', 'server_over50')})
  best = value.get('best')
  ready = [row for row in rows if row['n'] >= 1000 and row['seconds'] >= 60]
  if (value['state'] not in ('measuring', 'ready') or type(best) is not int
      or best not in {row['id'] for row in ready} or len(ready) < 2):
    best = None
  rates = {key: value[key] for key in ('source_fps', 'usb_fps', 'output_fps') if finite(value.get(key), 0, 120)}
  return {'version': 1, 'kind': kind, 'active': value['active'], 'effective': value['effective'], 'state': value['state'],
          'fallback': value['state'] == 'fallback', 'best': best, 'rows': rows, **rates}
