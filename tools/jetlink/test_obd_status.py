import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.common.obd_status import ObdBadgeStatus
from openpilot.selfdrive.carrot.obd_battery import config_id, configuration


@pytest.fixture
def badge(tmp_path):
  config = {'version': 1, 'address': '00:11:22:33:44:55', 'confirmed_fields': []}
  cfg, snapshot = tmp_path / 'config.json', tmp_path / 'snapshot.json'
  cfg.write_text(json.dumps(config))
  record = {'version': 1, 'config_id': config_id(configuration(config)), 'state': 'receiving', 'updated': 100.0,
            'metrics': {}, 'candidates': {}}
  snapshot.write_text(json.dumps(record))
  return ObdBadgeStatus(cfg, snapshot), record


def test_receiving_before_calibration_and_cached_expiry(badge):
  status, _ = badge
  assert status.update(104.9, True)
  status.snapshot_path.unlink()
  assert status.update(105.0, True)
  assert not status.update(105.01, True)
  assert not status.update(106.0, True)


@pytest.mark.parametrize('field,value', [
  ('state', 'connecting'), ('state', 'retry_exhausted'), ('state', 'disconnected'),
  ('state', 'unavailable'), ('state', 'stopped'), ('config_id', 'other'),
  ('version', 2), ('version', True), ('updated', True), ('updated', None),
  ('updated', '100'), ('updated', float('nan')), ('updated', float('inf')),
  ('updated', 101), ('updated', 94.9),
])
def test_reject_invalid_or_stale_snapshot(badge, field, value):
  status, record = badge
  record[field] = value
  status.snapshot_path.write_text(json.dumps(record))
  assert not status.update(100, True)


@pytest.mark.parametrize('target', ['config_path', 'snapshot_path'])
@pytest.mark.parametrize('content', ['{', '[]', 'null', ' ' * 4097])
def test_bounded_bad_files(badge, target, content):
  status, _ = badge
  getattr(status, target).write_text(content)
  assert not status.update(100, True)


def test_disable_and_config_change_clear(badge):
  status, _ = badge
  assert status.update(100, True)
  assert not status.update(100.1, False)
  status.config_path.unlink()
  assert not status.update(100.2, True)


@pytest.mark.parametrize('variant', ['onroad', 'mici/onroad'])
def test_renderers_draw_green_only_when_receiving(variant):
  # Exercise actual draw methods without loading native UI/hardware libraries.
  source = Path('openpilot/selfdrive/ui') / variant / 'hud_renderer.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudRenderer')
  method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_draw_bleobd_badge')
  assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == '_draw_bleobd_badge'
             for n in ast.walk(tree))
  boxes, texts = [], []
  color = (0, 255, 0, 210)
  rl = NS(Rectangle=lambda x, y, width, height: NS(x=x, y=y, width=width, height=height),
          Vector2=lambda x, y: NS(x=x, y=y), Color=lambda *args: args,
          draw_rectangle_rounded=lambda box, *args: boxes.append(box),
          draw_rectangle_rounded_lines_ex=lambda *args: None, draw_text_ex=lambda *args: texts.append(args))
  state = NS(bleobd_receiving=False)
  env = {'rl': rl, 'ui_state': state, 'COLORS': NS(GREEN_210=color),
         'UI_CONFIG': NS(border_size=30, button_size=192), 'measure_text_cached': lambda font, text, size: NS(x=6*size, y=size)}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), env)
  draw = env['_draw_bleobd_badge']
  rect = rl.Rectangle(10, 20, 1920, 1080)
  renderer = NS(_font_semi_bold='font')
  draw(renderer, rect)
  assert not texts and not boxes
  state.bleobd_receiving = True
  draw(renderer, rect)
  assert len(texts) == len(boxes) == 1
  assert texts[0][1] == 'BLEOBD' and texts[0][-1][1] == 255
  assert boxes[0].y >= rect.y + (56 if variant.startswith('mici') else 88)
  assert rect.x <= boxes[0].x < boxes[0].x + boxes[0].width <= rect.x + rect.width
