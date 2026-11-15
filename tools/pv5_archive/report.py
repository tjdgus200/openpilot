"""Reviewed summaries only. Completion requires the existing POST+GET receipt."""

import argparse
import json
from pathlib import Path
import time

from openpilot.system.pv5_incidents.archive_protocol import TOKEN
from tools.pv5_archive.store import Store
from tools.pv5_incidents.discord_report import Discord


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', type=Path, required=True)
  parser.add_argument('--policy', type=Path, required=True)
  parser.add_argument('--digest', required=True)
  parser.add_argument('--summary-file', type=Path, required=True)
  args = parser.parse_args()
  policy = json.loads(args.policy.read_text(encoding='utf-8'))
  if policy.get('enabled') is not True or policy.get('push_policy') != 'analysis_only':
    return
  if not TOKEN.fullmatch(args.digest) or args.summary_file.stat().st_size > 6000:
    raise ValueError('invalid_archive_report')
  content = args.summary_file.read_text(encoding='utf-8').strip()
  if not 1 <= len(content) <= 1900:
    raise ValueError('invalid_archive_report')
  config = json.loads(args.config.read_text(encoding='utf-8'))
  root = Path(policy['archive']['root'])
  store = Store(root, bytes.fromhex(config['key']))
  receipts = root / 'receipts'
  receipts.mkdir(exist_ok=True)
  config['discord_receipt'] = str(receipts / (args.digest + '.json'))
  discord = Discord(config)
  if discord.channel != '1559520538202738809':
    raise ValueError('unapproved_report_channel')
  with store.db() as db:
    if not db.execute('SELECT 1 FROM results WHERE segment_id=?', (args.digest,)).fetchone():
      raise ValueError('unknown_analysis')
    db.execute('CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, stamp REAL, receipt TEXT)')
  receipt = discord.send(content, [args.digest], 'analysis')
  if not receipt.get('verified') or receipt['digests'] != [args.digest] or receipt['content'] != content:
    raise ValueError('earlier_delivery_reconciled_retry_current_report')
  with store.db() as db:
    db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?)', (args.digest, time.time(), json.dumps(receipt)))
  print(json.dumps({'verified': True, 'message_id': receipt['message_id']}))


if __name__ == '__main__':
  main()
