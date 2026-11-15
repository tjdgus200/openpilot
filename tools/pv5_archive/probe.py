"""Authenticated invalid-schema probe: never inserts media or a fake incident."""

import argparse
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

from openpilot.system.pv5_incidents.archive_protocol import headers


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', type=Path, required=True)
  parser.add_argument('--public', action='store_true')
  args = parser.parse_args()
  cfg = json.loads(args.config.read_text(encoding='utf-8'))
  key, target = bytes.fromhex(cfg['key']), urlsplit(cfg['endpoint'])
  path, body = '/v1/archive/manifest', b'{"probe":1}'
  statuses = []
  for signed, tamper in ((False, False), (True, False), (True, True)):
    connection = (
      http.client.HTTPSConnection(target.hostname, target.port, timeout=10) if args.public else http.client.HTTPConnection('127.0.0.1', cfg['port'], timeout=3)
    )
    try:
      connection.request('POST', path, b'{"probe":2}' if tamper else body, headers=headers(key, path, body) if signed else {})
      response = connection.getresponse()
      statuses.append(response.status)
      response.read(8192)
    finally:
      connection.close()
  if statuses != [403, 422, 403]:
    raise RuntimeError('archive_probe_failed')
  print(
    json.dumps(
      {
        'public_https': args.public,
        'unauthorized_rejected': True,
        'authenticated_schema_checked': True,
        'tampered_rejected': True,
        'media_or_incident_inserted': False,
      }
    )
  )


if __name__ == '__main__':
  main()
