import json

import pytest

from tools.jetlink.check_pv5_models import REQUIRED, Results, checklist
from types import SimpleNamespace as NS


def test_checklist_requires_all_preservation_groups_and_real_suites(tmp_path):
  value = checklist()
  assert set(value['groups']) == REQUIRED
  value['groups'].pop('fov_center_hud_integration')
  path = tmp_path / 'incomplete.json'
  path.write_text(json.dumps(value))
  with pytest.raises(ValueError, match='incomplete'):
    checklist(path)


@pytest.mark.parametrize('problem', ['missing', 'failed', 'skipped', 'collection'])
def test_report_never_accepts_unrun_skipped_or_failed_checks(problem):
  result = Results()
  report = NS(nodeid='a.py::case', when='call', failed=False, skipped=False)
  if problem != 'missing':
    if problem in ('failed', 'skipped'):
      setattr(report, problem, True)
    result.pytest_runtest_logreport(report)
  if problem == 'collection':
    result.pytest_collectreport(NS(failed=True))
  assert result.failures or not result.group(['a.py'])['passed_all']


def test_complete_pc_group_is_distinct_from_physical_release_checks():
  result = Results()
  result.pytest_runtest_logreport(NS(nodeid='a.py::case', when='call', failed=False, skipped=False))
  assert result.group(['a.py']) == {'passed': 1, 'failed': 0, 'skipped': 0, 'passed_all': True}
  assert checklist()['release_checks']
