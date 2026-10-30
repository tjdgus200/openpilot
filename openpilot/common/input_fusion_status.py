"""Display-only validation of receive-complete -> TX-ready observations."""
import math


def status(value):
  if (not isinstance(value, dict) or value.get('version') != 1
      or value.get('scope') != 'rx_complete_to_tx_ready'
      or value.get('state') not in ('waiting', 'unsupported', 'warmup', 'measuring', 'ready')
      or any(type(value.get(k)) is not int or value[k] not in (0, 1) for k in ('requested', 'effective'))
      or type(value.get('change_allowed')) is not bool
      or not isinstance(value.get('rows'), list) or len(value['rows']) > 2):
    return None
  rows, seen = [], set()
  for row in value['rows']:
    if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] not in (0, 1)
        or row['id'] in seen or type(row.get('n')) is not int or not 1 <= row['n'] <= 6000
        or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k])
               or not 0 <= row[k] <= high for k, high in
               (('seconds', 3600), ('mean_ms', 10000), ('p99_ms', 10000), ('max_ms', 10000)))
        or row['max_ms'] < max(row['mean_ms'], row['p99_ms'])
        or type(row.get('over50')) is not int or not 0 <= row['over50'] <= row['n']):
      return None
    seen.add(row['id'])
    rows.append({key: row[key] for key in ('id', 'n', 'seconds', 'mean_ms', 'p99_ms', 'max_ms', 'over50')})
  reason = value.get('reason')
  reason = reason if reason in ('waiting', 'ready', 'disabled', 'unsupported', 'unavailable', 'probe_failed') else 'unsupported'
  failed = value.get('failed')
  failed = failed if type(failed) is int and 0 <= failed <= 1_000_000 else 0
  return {k: value[k] for k in ('version', 'scope', 'state', 'requested', 'effective', 'change_allowed')} | {
    'reason': reason, 'failed': failed, 'rows': rows}
