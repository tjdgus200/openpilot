"""Exercise actual presentation entry points without a graphics/device context."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pyray as rl
import pytest

from openpilot.common.hud_scc_popup import draw_scc_info_popup, is_scc_info_popup

ROOT = Path(__file__).resolve().parents[3]


def alert(kind='pv5SccDecelInfo/warning', status=0, size=1):
  return NS(alert_type=kind, status=status, size=size, text1='순정 감속 보조', text2='')


def method(path, name, scope):
  tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
  node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
  exec(compile(ast.Module([node], type_ignores=[]), str(path), 'exec'), scope)
  return scope[name]


@pytest.mark.parametrize('kind,status,size', [
  ('pv5SccDecel/warning', 2, 3), ('pv5SccDecelSilent/warning', 2, 3),
  ('pv5SccDecelInfo/warning', 2, 3), ('pv5SccDecelInfo/warning', 0, 0),
  ('pv5SccDecelInfo/permanent', 0, 1), ('fcw/permanent', 2, 3),
  ('driverDistracted/warning', 1, 2), ('', 2, 3), ('radarCutin/warning', 0, 1),
])
def test_only_exact_nonurgent_selected_scc_is_restyled(monkeypatch, kind, status, size):
  draw = Mock(); monkeypatch.setattr(rl, 'draw_rectangle_rounded', draw)
  assert not draw_scc_info_popup(alert(kind, status, size), rl.Rectangle(0, 0, 2160, 1080), Mock())
  draw.assert_not_called()
  assert not is_scc_info_popup(None)


@pytest.mark.parametrize('w,h,x,y', [(2160,1080,0,0),(2100,1020,30,30),(528,240,6,8),(1280,720,0,0)])
def test_popup_is_opaque_centered_and_inside_view(monkeypatch,w,h,x,y):
  fill=Mock(); edge=Mock(); text=Mock()
  monkeypatch.setattr(rl,'draw_rectangle_rounded',fill)
  monkeypatch.setattr(rl,'draw_rectangle_rounded_lines_ex',edge)
  assert draw_scc_info_popup(alert(),rl.Rectangle(x,y,w,h),text)
  box=fill.call_args.args[0]
  assert box.x+box.width/2 == pytest.approx(x+w/2)
  assert x <= box.x < box.x+box.width <= x+w
  assert y <= box.y < box.y+box.height <= y+h
  assert fill.call_args.args[-1].a == 245
  assert edge.call_args.args[-1].a == 255
  assert text.call_args.args[1] == '순정 감속 보조'


@pytest.mark.parametrize('compact',[False,True])
def test_actual_comma_render_routes_popup_before_legacy_text_without_delay(compact):
  popup=Mock(return_value=True)
  scope={'rl':rl,'draw_scc_info_popup':popup,'is_scc_info_popup':is_scc_info_popup,
         'ui_state':NS(sm=object()),'AlertStatus':NS(critical=2),'time':NS(monotonic=lambda:10)}
  path='selfdrive/ui/'+('mici/' if compact else '')+'onroad/alert_renderer.py'
  render=method(path,'_render',scope)
  a=alert(); obj=NS(get_alert=Mock(return_value=a), _attention_icon=NS(scale=Mock()),
                   _draw_scc_popup_text=Mock(), _prev_alert=a)
  render(obj,rl.Rectangle(0,0,528,240))
  assert popup.call_args.args[0] is a
  if compact: assert obj._prev_alert is None


def test_mici_removed_popup_does_not_fade_as_stale_text():
  scope={'rl':rl,'draw_scc_info_popup':draw_scc_info_popup,'is_scc_info_popup':is_scc_info_popup,'ui_state':NS(sm=None)}
  render=method('selfdrive/ui/mici/onroad/alert_renderer.py','_render',scope)
  obj=NS(get_alert=lambda sm:None, _prev_alert=alert(), _rect=rl.Rectangle(0,0,528,240),
         _draw_scc_popup_text=Mock(), _alpha_filter=NS(x=1, update=Mock()),_alert_y_filter=NS(update=Mock()))
  assert render(obj,obj._rect) is False
  assert obj._prev_alert is None


def test_actual_hud_keeps_critical_full_screen_and_balances_matrix():
  fake=NS(rl_push_matrix=Mock(),rl_pop_matrix=Mock(),rl_scalef=Mock(),draw_rectangle=Mock(),Rectangle=rl.Rectangle,Color=rl.Color)
  popup=Mock(return_value=True); scope={'rl':fake,'draw_scc_info_popup':popup,'ClusterAlert':object,
    'time':NS(monotonic=lambda:10),'DESIGN_WIDTH':1280,'DESIGN_HEIGHT':720,
    'CRITICAL_BACKGROUND':(200,20,20,255),'WHITE':object(),'draw_driver_attention_icon':Mock()}
  render=method('selfdrive/carrot/cluster/cluster_renderer.py','_draw_alert_overlay',scope)
  obj=NS(width=1280,height=720, _attention_icon=NS(scale=Mock(return_value=0),reset=Mock()),
         _draw_scc_popup_text=Mock(),_cluster_alert_text=lambda a:(a.text1,a.text2),
         _fit_alert_text=lambda text,size,*args:(text,size),_draw_text_with_stroke=Mock(),language='ko')
  render(obj,alert()); popup.assert_called_once();fake.rl_pop_matrix.assert_called_once()
  popup.reset_mock();fake.rl_pop_matrix.reset_mock()
  render(obj,alert('pv5SccDecel/warning',2,3))
  popup.assert_not_called();fake.draw_rectangle.assert_called_once();fake.rl_pop_matrix.assert_called_once()
