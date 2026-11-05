"""Repeatable PC-only model preservation gate. No device, download or inference.

Run from a prepared Python 3.12 Linux checkout. A pass means PC contract tests
passed; archive/ONNX/physical release checks remain explicitly separate.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = Path(__file__).with_name('pv5_model_checklist.json')
REQUIRED = {'identity_and_delivery', 'fov_center_hud_integration', 'hud_and_alerts',
            'battery_and_ble', 'resources_and_state', 'power_and_boot'}


def checklist(path=CHECKLIST):
  value = json.loads(path.read_text(encoding='utf-8'))
  if value.get('version') != 1 or set(value.get('groups', {})) != REQUIRED:
    raise ValueError('incomplete PV5 preservation checklist')
  for group in value['groups'].values():
    if not group.get('preserve') or not group.get('tests'):
      raise ValueError('empty PV5 preservation group')
    for name in group['tests']:
      item = Path(name)
      if item.is_absolute() or '..' in item.parts or not (ROOT / item).is_file() or item.suffix != '.py':
        raise ValueError('invalid preservation test path')
  if len(value.get('release_checks', [])) < 4:
    raise ValueError('release validation boundaries missing')
  return value


class Results:
  def __init__(self):
    self.files, self.failures = {}, 0

  def pytest_runtest_logreport(self, report):
    name = report.nodeid.split('::')[0].replace('\\', '/')
    row = self.files.setdefault(name, {'passed': 0, 'failed': 0, 'skipped': 0})
    if report.failed:
      row['failed'] += 1
      self.failures += 1
    elif report.skipped:
      row['skipped'] += 1
    elif report.when == 'call':
      row['passed'] += 1

  def pytest_collectreport(self, report):
    if report.failed:
      self.failures += 1

  def group(self, names):
    rows = [self.files.get(name, {}) for name in names]
    counts = {key: sum(row.get(key, 0) for row in rows) for key in ('passed', 'failed', 'skipped')}
    counts['passed_all'] = all(row.get('passed', 0) > 0 and not row.get('failed') and not row.get('skipped') for row in rows)
    return counts


def main(argv=None):
  if not __debug__:
    raise RuntimeError('Validation requires assertions; do not run Python with -O')
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--report', type=Path, help='Write the source/model-bound JSON result')
  parser.add_argument('--list', action='store_true', help='List permanent preservation requirements without running tests')
  args = parser.parse_args(argv)
  value = checklist()
  if args.list:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0
  if args.report:
    args.report.unlink(missing_ok=True)
  if Path('/TICI').exists() or Path('/etc/nv_tegra_release').exists():
    raise RuntimeError('Run this CPU-heavy validation on a PC, not comma or Jetson')
  paths = [ROOT, ROOT / 'tools/jetlink', ROOT / 'third_party/jetlink', ROOT / 'openpilot/selfdrive/carrot/cluster', ROOT / 'opendbc_repo']
  sys.path[:0] = list(map(str, paths))
  from openpilot.common import big_model_catalog as catalog
  import pytest
  models = {key: {'sha256': catalog.model_spec(key)['sha256'],
                  'package_source': catalog.usb_release(key)['source_commit']} for key in catalog.available()}
  head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
  dirty = bool(subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain']).strip())
  tests = list(dict.fromkeys(name for group in value['groups'].values() for name in group['tests']))
  result = Results()
  os.chdir(ROOT)
  code = pytest.main(['-c', os.devnull, '--rootdir=.', '--confcutdir=tools/jetlink', '--noconftest',
                      '-p', 'no:cacheprovider', '-q', '--tb=short', *tests], plugins=[result])
  groups = {name: result.group(group['tests']) for name, group in value['groups'].items()}
  passed = code == 0 and result.failures == 0 and all(group['passed_all'] for group in groups.values())
  report = dict(format=1, scope=value['scope'], source_commit=head, working_tree_dirty=dirty,
                pc_contracts_passed=passed, models=models, groups=groups,
                totals={key: sum(row[key] for row in result.files.values()) for key in ('passed', 'failed', 'skipped')},
                release_checks=[{'requirement': item, 'status': 'not_run_by_this_command'} for item in value['release_checks']],
                physical_device_tested=False)
  if args.report:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
  print(json.dumps({'pc_contracts_passed': passed, 'models': list(models), 'totals': report['totals']}, ensure_ascii=False))
  return 0 if passed else 1


if __name__ == '__main__':
  raise SystemExit(main())
