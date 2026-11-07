"""Exercise session boundaries with real Params and production manager blocks."""
import ast
from pathlib import Path
import runpy

import pytest

from openpilot.common import params as params_module
from openpilot.common.params import Params, ParamKeyFlag

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stores(tmp_path):
  persistent = Params(str(tmp_path / 'persistent'))
  memory = Params(str(tmp_path / 'memory'))
  memory.put('NavDestinationTemporary', '{"latitude":37,"longitude":127}')
  persistent.put('NavDestination', '{"latitude":38,"longitude":128}')
  return persistent, memory


def manager_block(function, predicate):
  path = ROOT / 'system/manager/manager.py'
  tree = ast.parse(path.read_text())
  function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
  node = next(n for n in ast.walk(function) if predicate(n))
  return compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec')


def test_manager_start_clears_only_temporary_destination(stores):
  persistent, memory = stores
  block = manager_block('manager_init', lambda n: isinstance(n, ast.Expr) and
                        'NavDestinationTemporary' in ast.unparse(n))
  exec(block, {'Params': lambda path: memory})
  assert memory.get('NavDestinationTemporary') is None
  assert persistent.get('NavDestination')


@pytest.mark.parametrize('started,previous,cleared', [(False, True, True), (True, False, False), (False, False, False)])
def test_manager_drive_boundary(stores, started, previous, cleared):
  persistent, memory = stores
  block = manager_block('manager_thread', lambda n: isinstance(n, ast.If) and
                        ast.unparse(n.test) == 'started and (not started_prev)')
  exec(block, {'Params': lambda path: memory, 'params': persistent, 'ParamKeyFlag': ParamKeyFlag,
               'started': started, 'started_prev': previous})
  assert (memory.get('NavDestinationTemporary') is None) == cleared
  assert persistent.get('NavDestination')


def test_cli_destination_replaces_temporary_destination(stores, monkeypatch):
  persistent, memory = stores
  monkeypatch.setattr(params_module, 'Params', lambda path=None: memory if path == '/dev/shm/params' else persistent)
  monkeypatch.setattr('sys.argv', ['set_destination.py', '36,126'])
  runpy.run_path(str(ROOT / 'selfdrive/navd/set_destination.py'), run_name='__main__')
  assert memory.get('NavDestinationTemporary') is None
  assert '126' in persistent.get('NavDestination')
