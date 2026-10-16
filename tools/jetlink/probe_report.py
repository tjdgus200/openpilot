"""Optional, expiring bench-only probe report; never changes probe acceptance."""
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


def destination(config):
  if set(config) != {'url', 'expires'} or time.time() >= config['expires']:
    return None
  url = urllib.parse.urlsplit(config['url'])
  if (url.scheme != 'http' or url.username or url.password or url.query or url.fragment
      or not url.port or not ipaddress.ip_address(url.hostname).is_private):
    raise ValueError('Probe report requires an explicit private IP endpoint')
  return config['url']


def run(probe):
  config = Path(__file__).with_name('probe_report.json')
  if '--probe-child' in sys.argv or not config.is_file():
    return probe()
  url = destination(json.loads(config.read_text()))
  if url is None:
    return probe()
  # The installed updater owns the process group and its existing timeout.
  # No credentials, environment, input tensors or journal are collected.
  with tempfile.TemporaryFile() as output:
    result = subprocess.run([sys.executable, *sys.argv, '--probe-child'], stdout=output, stderr=output)
    output.seek(max(0, output.tell() - 32768))
    tail = output.read().decode(errors='replace')
  print(tail, end='', flush=True)
  report = {'returncode': result.returncode, 'tail': tail}
  try:
    request = urllib.request.Request(url, data=json.dumps(report).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
      response.read(128)
  except Exception:
    pass  # Reporting cannot change the probe's exit status.
  raise SystemExit(result.returncode)
