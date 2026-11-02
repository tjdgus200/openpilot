import ast
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.common.params_watch import ParamsWatch

# Exercise the unchanged production method body without requiring a camera,
# native Params build or messaging transport in this file-backed unit test.
SOURCE = Path(__file__).parents[1] / 'carrot_serv.py'
TREE = ast.parse(SOURCE.read_text())
METHOD = next(n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef) and n.name == 'update_params')
namespace = {}
exec(compile(ast.Module(body=[METHOD], type_ignores=[]), str(SOURCE), 'exec'), namespace)
update_params = namespace['update_params']


class FileParams:
  def __init__(self, path):
    self.path, self.reads = path, 0

  def get_param_path(self):
    return str(self.path)

  def get(self, key):
    self.reads += 1
    try:
      return (self.path / key).read_text()
    except FileNotFoundError:
      return '0'

  def get_int(self, key):
    return int(self.get(key))

  def get_float(self, key):
    return float(self.get(key))

  def get_bool(self, key):
    return self.get(key) == '1'


def setting_reads():
  tree = ast.parse(SOURCE.read_text())
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'update_params')
  return {n.args[0].value for n in ast.walk(method) if isinstance(n, ast.Call) and n.args and
          isinstance(n.func, ast.Attribute) and n.func.attr.startswith('get') and isinstance(n.args[0], ast.Constant)}


def make_serv(path):
  serv = NS()
  serv.update_params = lambda: update_params(serv)
  serv.params = FileParams(path)
  serv._params_watch = ParamsWatch(serv.params, setting_reads())
  assert serv._params_watch.fd >= 0
  serv._params_loaded = False
  return serv


def test_unchanged_settings_no_reads_and_all_changes_next_tick(tmp_path):
  serv = make_serv(tmp_path)
  try:
    serv.update_params()
    first = serv.params.reads
    for _ in range(100):
      serv.update_params()
    assert serv.params.reads == first
    # Each settings read site is covered, including the fallback language key.
    for key in setting_reads():
      tmp = tmp_path / 'new'
      tmp.write_text('1')
      os.replace(tmp, tmp_path / key)
      before = serv.params.reads
      serv.update_params()
      assert serv.params.reads > before, key
    (tmp_path / 'VehicleNaviCanControl').write_text('2')
    serv.update_params()
    assert serv.vehicleNaviCanControl == 2
    (tmp_path / 'AutoNaviSpeedSafetyFactor').write_text('110')
    serv.update_params()
    assert serv.autoNaviSpeedSafetyFactor == 1.1
    # Every actual getter must be watched by the production constructor.
    tree = ast.parse(SOURCE.read_text())
    watch = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'ParamsWatch')
    assert setting_reads() == set(ast.literal_eval(watch.args[1]))
  finally:
    serv._params_watch.close()


def test_failed_read_is_retried_even_after_event_drained(tmp_path):
  serv = make_serv(tmp_path)
  try:
    (tmp_path / 'AutoCurveSpeedLowerLimit').write_text('bad')
    for _ in range(2):
      with pytest.raises(ValueError):
        serv.update_params()
      assert not serv._params_loaded
    (tmp_path / 'AutoCurveSpeedLowerLimit').write_text('15')
    serv.update_params()
    assert serv.autoCurveSpeedLowerLimit == 15
    serv._params_watch.close()
    before = serv.params.reads
    serv.update_params()
    serv.update_params()
    assert serv.params.reads >= before + 44
  finally:
    serv._params_watch.close()


def test_change_during_snapshot_is_seen_on_following_tick(tmp_path):
  serv = make_serv(tmp_path)
  original = serv.params.get
  changed = [False]

  def read(key):
    value = original(key)
    if key == 'AutoNaviSpeedSafetyFactor' and not changed[0]:
      changed[0] = True
      (tmp_path / key).write_text('110')
    return value

  serv.params.get = read
  try:
    serv.update_params()
    assert serv.autoNaviSpeedSafetyFactor == 0.
    serv.update_params()
    assert serv.autoNaviSpeedSafetyFactor == 1.1
  finally:
    serv._params_watch.close()
