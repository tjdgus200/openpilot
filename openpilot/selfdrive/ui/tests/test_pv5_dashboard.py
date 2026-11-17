import ast
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace as NS

import pytest

from openpilot.common.pv5_upload_status import (UploadStatus, StatusReader, collect, completed, worker_present,
                                               number, temperature, progress, lines, show_onroad)


def fixture(tmp_path):
  logs=tmp_path/'logs'; logs.mkdir()
  cat=tmp_path/'cat'; cat.mkdir()
  for name in ('route--0', 'route--1', 'route--2', 'boot'):
    (logs/name).mkdir()
  for name in ('archive.sqlite','archive-home-logs.sqlite'):
    with sqlite3.connect(cat/name) as db:
      db.execute('CREATE TABLE completed(segment TEXT PRIMARY KEY,inode INTEGER,mtime INTEGER)')
  (cat/'archive.lock').touch()
  locks=tmp_path/'locks'; locks.write_text('')
  return logs,cat,locks


def ack(cat,log_root,name,db_name):
  meta=(log_root/name).stat()
  with sqlite3.connect(cat/db_name) as db:
    db.execute('INSERT INTO completed VALUES(?,?,?)',(name,meta.st_ino,meta.st_mtime_ns))


def test_only_receiver_acknowledged_matching_current_directories(tmp_path):
  logs,cat,locks=fixture(tmp_path)
  ack(cat,logs,'route--0','archive-home-logs.sqlite')
  ack(cat,logs,'route--0','archive.sqlite')
  ack(cat,logs,'route--1','archive-home-logs.sqlite')
  before={p.name:p.read_bytes() for p in cat.glob('*.sqlite')}
  status=collect(logs,cat,locks)
  assert (status.total,status.logs,status.full,status.working)==(3,2,1,False)
  assert before=={p.name:p.read_bytes() for p in cat.glob('*.sqlite')}
  (logs/'route--1'/'new-recording').touch()
  status=collect(logs,cat,locks)
  assert status.logs==1 and status.full==1
  assert '100%' not in progress('logs',status.logs,status.total)


def test_missing_database_is_unknown_and_not_created(tmp_path):
  assert completed(tmp_path/'missing',{}) is None
  assert not (tmp_path/'missing').exists()


def test_locked_database_is_unknown_not_complete(tmp_path):
  logs,cat,locks=fixture(tmp_path)
  db=sqlite3.connect(cat/'archive.sqlite')
  try:
    db.execute('BEGIN EXCLUSIVE')
    status=collect(logs,cat,locks)
    assert status.full is None and status.logs is None
  finally:
    db.rollback();db.close()


def test_oversized_or_bad_catalog_is_unknown(tmp_path):
  p=tmp_path/'bad';p.write_bytes(b'not a database')
  assert completed(p,{}) is None
  with p.open('wb') as f:f.truncate(2*1024**2+1)
  assert completed(p,{}) is None


def test_scan_cancels_on_motion_without_examining_recordings(tmp_path):
  logs,cat,locks=fixture(tmp_path)
  assert collect(logs,cat,locks,allowed=lambda:False).total is None


def test_scan_limit_returns_unknown(tmp_path,monkeypatch):
  import openpilot.common.pv5_upload_status as m
  logs,cat,locks=fixture(tmp_path)
  monkeypatch.setattr(m,'LIMIT',2)
  assert collect(logs,cat,locks).total is None


@pytest.mark.skipif(sys.platform!='linux',reason='actual Linux flock observation')
def test_real_lock_is_observed_without_interfering(tmp_path):
  import fcntl
  p=tmp_path/'archive.lock'
  with p.open('w') as f:
    assert worker_present(p,Path('/proc/locks')) is False
    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert worker_present(p,Path('/proc/locks')) is True
    with p.open('r') as other:
      with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
    fcntl.flock(f,fcntl.LOCK_UN)
    assert worker_present(p,Path('/proc/locks')) is False


def ui_fixture():
  sm=NS()
  class SM(dict):pass
  sm=SM(deviceState=NS(powerDrawW=12.4,cpuTempC=[50.,58.],gpuTempC=[52.],networkType='wifi'),
    carParams=NS(passive=True,dashcamOnly=False),
    carState=NS(canValid=True,canTimeout=False,standstill=True,vEgoRaw=0.,vEgo=0.,cruiseState=NS(enabled=False)),
    selfdriveState=NS(enabled=False,active=False),carControl=NS(enabled=False,latActive=False,longActive=False),
    pandaStates=[NS(pandaType='tres',harnessStatus='notConnected',ignitionLine=False,ignitionCan=False)],
    managerState=NS(processes=[NS(name='loggerd',running=True),NS(name='encoderd',running=True)]))
  sm.seen={k:True for k in sm};sm.valid=sm.seen.copy();sm.recv_time={k:99.9 for k in sm}
  return NS(sm=sm,started=True,dashcam_mode=False)


@pytest.mark.parametrize('field,value',[('powerDrawW',float('nan')),('powerDrawW',0),('powerDrawW',-1),('cpuTempC',[]),('gpuTempC',[float('inf')])])
def test_unknown_measurements_never_become_zero(field,value):
  ui=ui_fixture();setattr(ui.sm['deviceState'],field,value)
  rendered=lines(UploadStatus(99,3,1,0,True),ui.sm,100)
  assert '—' in rendered[3]


def test_live_power_and_max_temperature():
  text=lines(UploadStatus(99,3,1,0,True),ui_fixture().sm,100)
  assert '12.4 W' in text[3] and '58°C' in text[3] and '52°C' in text[3]
  assert '준비·전송' in text[0] and '분석은 별도' in text[-1]


def test_stale_values_hide_previous_activity_and_measurements():
  text=lines(UploadStatus(1,3,3,3,True),ui_fixture().sm,200)
  assert '확인' in text[0] and '100%' not in ''.join(text) and '12.4' not in ''.join(text)


@pytest.mark.parametrize('case',['alert','moving','cruise','controls','selfdrive','not_dashcam','stale','unknown_panda','stale_controls'])
def test_onroad_panel_never_covers_active_driving_or_alerts(case):
  ui=ui_fixture()
  if case=='moving':ui.sm['carState'].vEgoRaw=1
  if case=='cruise':ui.sm['carState'].cruiseState.enabled=True
  if case=='controls':ui.sm['carControl'].latActive=True
  if case=='selfdrive':ui.sm['selfdriveState'].enabled=True
  if case=='not_dashcam':ui.sm['carParams'].passive=False
  if case=='stale':ui.sm.recv_time['deviceState']=90
  if case=='stale_controls':ui.sm.recv_time['carControl']=90
  if case=='unknown_panda':
    ui.sm['carState'].canValid=False;ui.sm['pandaStates'][0].pandaType='unknown'
  assert not show_onroad(ui,100,alert=case=='alert')


def test_stationary_and_physically_detached_dashcam():
  ui=ui_fixture();assert show_onroad(ui,100)
  ui.sm['carState'].canValid=False
  assert show_onroad(ui,100)
  ui.sm['pandaStates']=[]
  assert not show_onroad(ui,100)


def test_reader_never_blocks_render_and_stops_idle_polling():
  import threading
  entered=threading.Event();finish=threading.Event();calls=[]
  def loader(allowed):
    calls.append(1);entered.set();finish.wait(1)
    return UploadStatus(time.monotonic(),1,0,0,False)
  reader=StatusReader(loader)
  start=time.monotonic();old=reader.get(start)
  assert time.monotonic()-start<.1 and old.total is None
  assert entered.wait(1)
  reader.visible_until=0.;finish.set()
  time.sleep(.05)
  for _ in range(30):reader.get(time.monotonic())
  assert len(calls)==1
  reader.visible_until=0.


def renderer(width,height):
  from openpilot.common import pv5_upload_status as model
  ui=ui_fixture();draw=[]
  ns={'time':NS(monotonic=lambda:100.),'ui_state':ui,
      '_reader':NS(get=lambda now:UploadStatus(99.,315,200,20,True)),
      'lines':model.lines,'show_onroad':model.show_onroad,
      'rl':NS(Rectangle=lambda x,y,width,height:NS(x=x,y=y,width=width,height=height),Color=lambda *v:v,
              WHITE=(255,255,255,255),draw_rectangle_rounded=lambda *v:None),
      'gui_label':lambda rect,text,size,color:draw.append((rect,text,size,color))}
  path=Path(__file__).parents[1]/'widgets/pv5_dashboard.py'
  fn=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef))
  exec(compile(ast.Module(body=[fn],type_ignores=[]),str(path),'exec'),ns)
  rect=ns['rl'].Rectangle(0,0,width,height)
  return ns,rect,draw


@pytest.mark.parametrize('width,height,onroad',[(900,700,False),(520,184,False),(2160,1080,True),(536,240,True)])
def test_panel_rows_fit_big_and_small_screens(width,height,onroad):
  ns,rect,draw=renderer(width,height)
  ns['render_dashboard'](rect,onroad=onroad)
  assert len(draw)==8
  for r,text,size,color in draw:
    assert 0<=r.x<r.x+r.width<=width and 0<=r.y<r.y+r.height<=height
    assert size<=r.height


def test_alert_has_priority_and_stops_background_scan():
  ns,rect,draw=renderer(536,240)
  ns['render_dashboard'](rect,onroad=True,alert=True)
  assert not draw and ns['_reader'].visible_until==0.


def test_both_camera_views_draw_panel_before_alert():
  root=Path(__file__).parents[1]
  for name,alert_call in [('onroad/augmented_road_view.py',"timing.call('alert'"),('mici/onroad/augmented_road_view.py','self._alert_renderer.render(')]:
    source=(root/name).read_text(encoding='utf-8')
    assert source.index('render_dashboard(self._content_rect')<source.index(alert_call)


def test_normal_home_is_preserved_but_pv5_or_dashcam_shows_status():
  from openpilot.common.pv5_upload_status import show_home
  assert not show_home(NS(CP=None))
  assert not show_home(NS(CP=NS(carFingerprint='OTHER'),dashcam_mode=False))
  assert show_home(NS(CP=NS(carFingerprint='KIA_PV5'),dashcam_mode=False))
  assert show_home(NS(CP=None,dashcam_mode=True))


@pytest.mark.parametrize('flag',['stockAeb','stockFcw'])
def test_stock_emergency_warning_hides_panel(flag):
  ui=ui_fixture();setattr(ui.sm['carState'],flag,True)
  assert not show_onroad(ui,100)


def test_dashboard_is_behind_hud_including_scc_popups():
  root=Path(__file__).parents[1]
  for name,hud in [('onroad/augmented_road_view.py',"timing.call('hud'"),('mici/onroad/augmented_road_view.py','self._hud_renderer.render(')]:
    source=(root/name).read_text(encoding='utf-8')
    assert source.index('render_dashboard(self._content_rect')<source.index(hud)


def test_video_only_completion_never_counts_as_log(tmp_path):
  logs,cat,locks=fixture(tmp_path)
  ack(cat,logs,'route--0','archive.sqlite')
  status=collect(logs,cat,locks)
  assert status.full==1 and status.logs==0
  (logs/'route--1'/'rlog.zst').write_bytes(b'fixture')
  ack(cat,logs,'route--1','archive.sqlite')
  status=collect(logs,cat,locks)
  assert status.full==2 and status.logs==1
