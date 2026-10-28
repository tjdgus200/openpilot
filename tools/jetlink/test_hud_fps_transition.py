"""PC replay of Web Params -> comma snapshot -> USB receiver -> HUD selection.

Virtual time includes time spent receiving carState, unlike the original
zero-time fake. No physical display, USB bandwidth or GPU timing claim.
"""
import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from hud_rate import RATES, PARAM, selection
import hud_protocol as p


def builder_at_realistic_receive_time(monkeypatch):
  from openpilot.cereal import log
  import openpilot.common.jetlink_status as status
  clock, selected = [100.], [0]
  state = NS(gear='park', speed=0., age=0., valid=True, alive=True)
  class SM:
    updated = dict.fromkeys(p.SERVICES, False) | {'carState': True}
    valid = dict.fromkeys(p.SERVICES, False)
    alive = dict.fromkeys(p.SERVICES, False)
    recv_time, logMonoTime = {}, {}
    def update(self, _):
      clock[0] += .002  # SubMaster records arrivals inside update(), after entry.
      self.recv_time['carState'] = clock[0] - state.age
      self.logMonoTime['carState'] = int(clock[0] * 1e9)
      self.valid['carState'], self.alive['carState'] = state.valid, state.alive
      self.car = log.Event.new_message(carState={'gearShifter':state.gear,'vEgo':state.speed}).carState
    def __getitem__(self, _):
      return self.car
  builder = object.__new__(p.SnapshotBuilder)
  builder.sm, builder.params = SM(), NS(get=lambda key: {
    PARAM:str(selected[0]).encode(), 'IsOnroad':b'1'}.get(key))
  builder.next_send = builder.next_params = 0.
  builder.cached, builder.settings = {}, {}
  builder.target_fps = builder.requested_fps = 10
  builder.rate_control = True
  builder.host_release = builder.nav_map = None
  builder.network = NS(address=lambda:'')
  builder.camera_previews = dict
  builder.battery_snapshot = lambda _:None
  monkeypatch.setattr(p.time,'monotonic',lambda:clock[0])
  monkeypatch.setattr(status,'badge',lambda:None)
  return builder,clock,selected,state


@pytest.mark.parametrize('fps', RATES)
def test_web_request_crosses_real_receive_clock_and_reaches_host(tmp_path,monkeypatch,fps):
  import hud
  import server
  builder,clock,selected,_ = builder_at_realistic_receive_time(monkeypatch)
  selected[0] = RATES.index(fps)
  source = object.__new__(p.Publisher)
  source.path,source.last_sent,source.rate_control = tmp_path/'source',0.,True
  session = object.__new__(server.CarrotSession)
  session.last_seq,session.usb_display = 0,True
  host_path = tmp_path/'host'
  monkeypatch.setattr(server,'HUD_PACKET',host_path)
  monkeypatch.setattr(p,'HUD_PACKET',host_path)
  monkeypatch.setattr(p,'_next_snapshot_read',0.)
  host = hud.DisplayParams()
  stamps=[]
  for seq in range(1,fps*3+1):
    raw=builder.packet()
    assert raw is not None
    stamp=clock[0]
    source.path.write_bytes(p.HEADER.pack(stamp)+p.PUBLISHER_RATE.pack(p.RATE_MAGIC,builder.target_fps)+raw)
    packet=source.packet()
    assert source.target_fps == fps
    session.handle(NS(msg_type=p.HUD_MESSAGE,seq=seq,payload=packet))
    assert host.get_int(PARAM) == RATES.index(fps)
    assert source.packet() is None  # Same snapshot is never replayed.
    stamps.append(stamp)
    clock[0]=builder.next_send
  assert (len(stamps)-1)/(stamps[-1]-stamps[0]) == pytest.approx(fps)
  record=json.loads(host_path.read_bytes()[p.HEADER.size:])
  assert record['sent'] >= record['received']['carState']
  assert int(base64.b64decode(record['params'][PARAM])) == RATES.index(fps)


@pytest.mark.parametrize('field,value', [('gear','drive'),('speed',.01),('age',.301),
                                       ('age',-.001),('valid',False),('alive',False)])
def test_display_preference_does_not_reclassify_motion_or_invalid_vehicle_data(monkeypatch,field,value):
  builder,_,selected,state=builder_at_realistic_receive_time(monkeypatch)
  selected[0]=4
  setattr(state,field,value)
  packet=json.loads(builder.packet())
  assert packet['params'][PARAM] == 'NA=='
  assert builder.target_fps == 30
  assert packet['valid']['carState'] == state.valid
  assert packet['alive']['carState'] == state.alive
  assert packet['received']['carState'] == builder.sm.recv_time['carState']


def test_saved_selection_applies_onroad_in_drive_and_returns_to_ten(monkeypatch):
  builder,clock,selected,state=builder_at_realistic_receive_time(monkeypatch)
  selected[0],state.gear=4,'drive'
  assert json.loads(builder.packet())['params'][PARAM] == 'NA=='
  clock[0]=builder.next_send
  state.gear='park'
  assert json.loads(builder.packet())['params'][PARAM] == 'NA=='
  clock[0]+=1.
  selected[0],state.gear=0,'drive'
  assert json.loads(builder.packet())['params'][PARAM] == 'MA=='


@pytest.mark.parametrize('fps', RATES)
def test_publisher_restart_onroad_keeps_saved_choice_without_waiting_for_park(monkeypatch,fps):
  for _ in range(2):
    builder,_,selected,state=builder_at_realistic_receive_time(monkeypatch)
    selected[0],state.gear,state.speed=RATES.index(fps),'drive',15.
    assert json.loads(builder.packet())['params'][PARAM] == base64.b64encode(str(selected[0]).encode()).decode()
    assert builder.target_fps == fps


def test_actual_host_restart_loop_applies_each_admitted_fps():
  import hud_quality as quality
  source=Path(__file__).with_name('hud.py')
  tree=ast.parse(source.read_text(encoding='utf-8'))
  main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
  loop=next(n for n in reversed(main.body) if isinstance(n,ast.While))
  class Owner:
    def __enter__(self):
      return self
    def __exit__(self,*args):
      pass
  choices,commands,position=[0,1,2,3,4,0],[],[0]
  def render(**kwargs):
    commands.append(list(env['sys'].argv))
    position[0]+=1
  env={'selection':selection,'PARAM':PARAM,'override':None,'requested':0,
       'settings':NS(get_int=lambda _:choices[min(position[0],len(choices)-1)]),
       'profile_allowed':lambda _:True,'hud_quality':NS(),'hud_runtime_stats':NS(),
       'sys':NS(argv=[]),'base':['hud.py'],'codec_options':quality.codec_options,
       'NAMES':quality.NAMES,'RATES':RATES,'extra_args':[],'cluster':NS(main=render),
       'panel_owner':lambda **kwargs:Owner(),'CpuJpegError':quality.CpuJpegError,
       'QualityWatch':lambda:NS(read=lambda:int(position[0]<len(choices)))}
  runner=ast.FunctionDef(name='run',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],
    kw_defaults=[],defaults=[]),body=[loop],decorator_list=[])
  exec(compile(ast.fix_missing_locations(ast.Module(body=[runner],type_ignores=[])),str(source),'exec'),env)
  env['run']()
  assert [args[-1] for args in commands] == ['10','15','20','25','30','10']
  assert all(args[args.index('--usb-jpeg-quality')+1]=='92' for args in commands)
