"""Exercise live HUD telemetry from the installed server's launch directory."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('scenario', ['fresh', 'optional_import_missing', 'stale', 'invalid_quality'])
def test_fresh_telemetry_without_repository_pythonpath(scenario):
  env = os.environ.copy()
  # Keep external PC dependency directories, never this checkout or its modules.
  paths = [p for p in env.get('PYTHONPATH', '').split(os.pathsep)
           if p and not Path(p).resolve().is_relative_to(ROOT)]
  env['PYTHONPATH'] = os.pathsep.join(paths)
  env['PYTHONDONTWRITEBYTECODE'] = '1'
  code = r'''
import builtins
import json
import sys
import time
from types import SimpleNamespace as NS
import server

scenario = sys.argv[1]
quality = {'version': 1, 'active': 0, 'effective': 0, 'state': 'waiting', 'rows': []}
if scenario == 'invalid_quality':
  quality['active'] = 999
status = {'updated': time.monotonic() - (5 if scenario == 'stale' else 0), 'quality': quality}
class StatusPath:
  def __init__(self, path):
    assert str(path) == '/dev/shm/carrot-jetlink-hud-status.json'
  def read_text(self):
    return json.dumps(status)
server.Path = StatusPath
base_import = builtins.__import__
def controlled_import(name, *args, **kwargs):
  if scenario == 'optional_import_missing' and name == 'openpilot.common.hud_quality_status':
    raise ModuleNotFoundError('optional HUD diagnostic unavailable')
  return base_import(name, *args, **kwargs)
builtins.__import__ = controlled_import
telemetry = object.__new__(server.DisplayTelemetry)
telemetry.original = NS(read=lambda: {'original': 'kept'})
telemetry.health = NS(read=lambda: {'severity': 'ok'})
for _ in range(3):
  value = telemetry.read()
  assert value['original'] == 'kept'
  assert value['carrot_health'] == {'severity': 'ok'}
  assert value['carrot_hud_connected'] == (scenario != 'stale')
  assert ('carrot_hud_quality' in value) == (scenario == 'fresh')
print('FRESH_TELEMETRY_OK', scenario)
'''
  result = subprocess.run([sys.executable, '-c', code, scenario], cwd=ROOT / 'tools/jetlink',
                          env=env, capture_output=True, text=True, timeout=20)
  assert result.returncode == 0, result.stderr[-2500:]
  assert f'FRESH_TELEMETRY_OK {scenario}' in result.stdout
