"""Check actual C3X PV5 geometry without a graphics context or device."""

import ast
from pathlib import Path

import pytest
import pyray as rl

SOURCE = Path(__file__).resolve().parents[1] / 'onroad/alert_renderer.py'


@pytest.mark.parametrize('width,height,x,y', [(2100, 1020, 30, 30), (2160, 1080, 0, 0), (1600, 900, 17, 22)])
@pytest.mark.parametrize('panel_height', [191, 340])
def test_actual_dm_panel_is_centered_in_bounds_and_above_largest_speed_box(width, height, x, y, panel_height):
  tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
  node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'pv5_attention_rect')
  scope = {'rl': rl, 'ALERT_MARGIN': 40}
  exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), scope)
  view = rl.Rectangle(x, y, width, height)
  panel = rl.Rectangle(x + 40, y + height - panel_height - 40, width - 80, panel_height)
  result = scope['pv5_attention_rect'](view, panel)
  assert result.x + result.width / 2 == pytest.approx(x + width / 2)
  assert result.x >= x + 40 and result.x + result.width <= x + width - 40
  assert result.y >= y + 40 and result.y + result.height <= y + height - 500 - 20
  assert result.height == panel_height
