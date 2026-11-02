"""Render-command checks for synthetic data; not a physical comma screen test."""
import importlib.util
import ast
from pathlib import Path
import sys
import re
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_debug as d
from test_obd_debug import display, monitor, reply


def load_ui(monkeypatch, calls):
  fake = NS(Rectangle=lambda x, y, w, h: NS(x=x, y=y, width=w, height=h),
            Vector2=lambda x, y: NS(x=x, y=y), Color=lambda r, g, b, a: (r, g, b, a),
            draw_rectangle_rounded=lambda *args: calls.append(('rect', args)),
            draw_text_ex=lambda *args: calls.append(('text', args)))
  monkeypatch.setitem(sys.modules, 'pyray', fake)
  monkeypatch.setitem(sys.modules, 'openpilot.system.ui.lib.text_measure',
                      NS(measure_text_cached=lambda font, text, size: NS(x=len(text) * size * .65)))
  path = Path(__file__).parents[2] / 'openpilot/selfdrive/ui/onroad/obd_debug.py'
  spec = importlib.util.spec_from_file_location('_raw_obd_ui_test', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, 'time', NS(monotonic=lambda: 100.))
  return module, fake


def setup(monkeypatch, tmp_path, *, status=True):
  calls = []
  module, fake = load_ui(monkeypatch, calls)
  overlay = module.ObdDebugOverlay()
  overlay.display, value = display(tmp_path)
  if status:
    d.atomic_write(overlay.display.status, {'version': 1, 'nonce': value['nonce'], 'config_id': value['config_id'],
      'updated': 100., 'rows': {key: {'state': 'ok', 'at': 100., 'data': d.parse(reply(key), key).hex().upper()}
                              for key, *_ in d.TARGETS}})
  state = NS(CP=NS(notCar=False, carFingerprint='KIA_PV5'), started=True, sm=monitor())
  return overlay, calls, fake.Rectangle(0, 0, 1920, 1080), state


@pytest.mark.parametrize('failure', ['timeout', 'unavailable', 'no_data'])
def test_one_missing_ecu_page_is_blank_without_hiding_the_other_pages(monkeypatch, tmp_path, failure):
  overlay, calls, rect, state = setup(monkeypatch, tmp_path)
  record = d.read(overlay.display.status)
  record['rows']['7EC/0101'] = {'state': failure, 'at': 100.}
  d.atomic_write(overlay.display.status, record)
  assert overlay.draw(rect, None, state)
  labels = [args[1] for kind, args in calls if kind == 'text']
  assert any(label.startswith('ID:7EC/0101') and label.endswith(failure.upper()) for label in labels)
  assert labels.count('DATA: --') == 1
  assert sum(len(label.split()) for label in labels if re.fullmatch(r'[0-9A-F]{2}(?: [0-9A-F]{2})*', label)) == 232 - 59


@pytest.mark.parametrize('width', [1200, 1600, 1920, 2160])
def test_all_six_full_pages_fit_without_truncating_bytes(monkeypatch, tmp_path, width):
  overlay, calls, rect, state = setup(monkeypatch, tmp_path)
  rect.width = width
  rect.x, rect.y = 70, 42
  assert overlay.draw(rect, None, state) is True
  labels = [args[1] for kind, args in calls if kind == 'text']
  for key, tx, *_ in d.TARGETS:
    assert any(label.startswith(f'ID:{key} TX:{tx}') for label in labels)
  expected_bytes = sum(length - 3 for *_, length in d.TARGETS)
  assert sum(len(label.split()) for label in labels if re.fullmatch(r'[0-9A-F]{2}(?: [0-9A-F]{2})*', label)) == expected_bytes
  panel = calls[0][1][0]
  assert (panel.x, panel.y, panel.width, panel.height) == (rect.x, rect.y, rect.width, rect.height)
  assert calls[0][1][-1][-1] == 255
  for kind, args in calls:
    if kind != 'text':
      continue
    _, label, position, size, _, _ = args
    assert panel.x < position.x < panel.x + panel.width - 5
    assert panel.y < position.y <= panel.y + panel.height - size
    # Conservative average ASCII width, including the longer footer/header.
    assert position.x + len(label) * size * .65 <= panel.x + panel.width
    if re.fullmatch(r'[0-9A-F]{2}(?: [0-9A-F]{2})*', label):
      assert size >= 20


@pytest.mark.parametrize('fault', ['moving', 'other_car', 'offroad', 'timeout'])
def test_overlay_hidden_immediately_on_vehicle_or_control_loss(monkeypatch, tmp_path, fault):
  overlay, calls, rect, state = setup(monkeypatch, tmp_path)
  if fault == 'moving':
    state.sm['carState'].vEgoRaw = 1.
  elif fault == 'other_car':
    state.CP.carFingerprint = 'OTHER'
  elif fault == 'offroad':
    state.started = False
  else:
    state.sm['carState'].canTimeout = True
  overlay.draw(rect, None, state)
  assert calls == []


def test_pending_collector_is_visible_without_invented_values(monkeypatch, tmp_path):
  overlay, calls, rect, state = setup(monkeypatch, tmp_path, status=False)
  overlay.draw(rect, None, state)
  labels = [args[1] for kind, args in calls if kind == 'text']
  assert labels.count('DATA: --') == 6
  assert any('normal battery polling paused' in label for label in labels)


def test_change_highlight_is_per_byte_not_a_decoded_signal(monkeypatch, tmp_path):
  overlay, calls, rect, state = setup(monkeypatch, tmp_path)
  overlay.draw(rect, None, state)
  calls.clear()
  overlay.display.changed['7BB/0100', 2] = 99.
  overlay.draw(rect, None, state)
  colors = [args[-1] for kind, args in calls if kind == 'text' and len(args[1]) == 2]
  assert colors.count((255, 210, 40, 255)) == 1


@pytest.mark.parametrize('variant', ['onroad', 'mici/onroad'])
def test_active_full_screen_diagnostic_bypasses_normal_hud_paint_only(variant):
  path = Path(__file__).parents[2] / 'openpilot/selfdrive/ui' / variant / 'hud_renderer.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudRenderer')
  method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_render')
  calls = []
  state, rect = object(), object()
  env = {'rl': NS(Rectangle=object), 'ui_state': state}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), env)
  renderer = NS(_font_semi_bold='font', _obd_debug=NS(draw=lambda *args: calls.append(args) or True))
  env['_render'](renderer, rect)
  assert calls == [(rect, 'font', state)]
