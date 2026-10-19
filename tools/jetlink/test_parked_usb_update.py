"""Onroad is not permission to update a moving/engaged vehicle."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_usb_release import env, Wire, cache_assets
from openpilot.selfdrive.modeld.jetlink import startup, update_permission as policy
from tools.jetlink import usb_release as usb


class Sample:
  def __init__(self):
    self.messages = {
      'carState': NS(canValid=True,canTimeout=False,standstill=True,vEgo=0.,gearShifter='park',cruiseState=NS(enabled=False)),
      'selfdriveState': NS(enabled=False,active=False),
      'carControl': NS(enabled=False,latActive=False,longActive=False),
    }
    self.valid = dict.fromkeys(policy.SERVICES, True)
    self.alive = dict.fromkeys(policy.SERVICES, True)
    self.recv_time = dict.fromkeys(policy.SERVICES, 100.)
  def __getitem__(self, key): return self.messages[key]
  def update(self, timeout): assert timeout == 0


def params(offroad=False,onroad=True):
  return NS(get_bool=lambda k: {'IsOffroad':offroad,'IsOnroad':onroad}[k])


@pytest.mark.parametrize('field,value', [('standstill',False),('vEgo',.02),('vEgo',float('nan')),
                                       ('gearShifter','drive'),('gearShifter','reverse'),('canValid',False),('canTimeout',True)])
def test_motion_gear_or_bad_can_denies_permission(field,value):
  sm=Sample(); setattr(sm['carState'],field,value)
  assert not policy.parked_inactive(sm,100.1)


@pytest.mark.parametrize('service,field', [('selfdriveState','enabled'),('selfdriveState','active'),
                                        ('carControl','enabled'),('carControl','latActive'),('carControl','longActive')])
def test_any_assistance_activity_denies_permission(service,field):
  sm=Sample(); setattr(sm[service],field,True)
  assert not policy.parked_inactive(sm,100.1)


def test_stock_acc_also_denies_permission():
  sm=Sample(); sm['carState'].cruiseState.enabled=True
  assert not policy.parked_inactive(sm,100.1)


@pytest.mark.parametrize('service',policy.SERVICES)
@pytest.mark.parametrize('failure', ['invalid','dead','old','future'])
def test_each_service_must_be_valid_alive_and_recent(service,failure):
  sm=Sample()
  if failure=='invalid': sm.valid[service]=False
  if failure=='dead': sm.alive[service]=False
  if failure=='old': sm.recv_time[service]=99.
  if failure=='future': sm.recv_time[service]=101.
  assert not policy.parked_inactive(sm,100.1)


def test_permission_does_not_rewrite_params_or_vehicle_data_and_fails_on_missing_subscriber():
  sm=Sample()
  guard=policy.Permission(params(),factory=lambda:sm,clock=lambda:100.1)
  assert guard.allowed()
  sm['carState'].gearShifter='drive'
  assert not guard.allowed()  # Even inside the 50ms poll interval.
  assert sm['carState'].gearShifter=='drive'
  denied=policy.Permission(params(),factory=lambda:None,clock=lambda:100.1)
  assert not denied.allowed()
  off=policy.Permission(params(True,False),factory=lambda:pytest.fail('offroad must not need CAN'))
  assert off.allowed()
  assert not policy.Permission(params(True,True)).allowed()
  assert not policy.Permission(params(False,False)).allowed()
  assert not policy.Permission(params(),factory=lambda:sm,allow_parked=False).allowed()


def prepare_wire(env,monkeypatch,sm):
  monkeypatch.setattr(startup,'selected_release',lambda *a,**k:env.value)
  monkeypatch.setattr(startup,'RELEASE',env.tmp/'host_release.json')
  (env.tmp/'usb_host_public.pem').write_bytes(env.public.read_bytes())
  cache=env.tmp/'comma-cache'; cache_assets(env,cache)
  # Only remap the cache path; no /data writes or real network/USB operations.
  original_upload=__import__('tools.jetlink.usb_release_client',fromlist=['upload']).upload
  import tools.jetlink.usb_release_client as sender
  monkeypatch.setattr(sender,'upload',lambda client,value,key,ignored,allowed,progress:
                      original_upload(client,value,key,cache,allowed,progress))
  monkeypatch.setattr(sender.urllib.request,'urlopen',lambda *a,**k:pytest.fail('offline test attempted network'))
  monkeypatch.setitem(sys.modules,'openpilot.common.params',NS(Params=lambda:params()))
  clock=[100.1]
  monkeypatch.setattr(startup,'Permission',lambda p,**kw:policy.Permission(p,factory=lambda:sm,clock=lambda:clock[0],**kw))
  wire=Wire()
  wire.clock=clock
  wire.send_json=lambda kind,seq,value:wire.send(kind,seq,[json.dumps(value).encode()])
  wire.client.state=lambda:{'carrot_update':{'state':'ready'}}
  peer={'carrot_host':'jetson',startup.CAPABILITY:True,usb.CAPABILITY:True,'usb_source_commit':'a'*40,
        'engine_state':'none','loaded':None}
  return wire,peer


def test_onroad_parked_boot_gate_upload_works_without_wifi_or_internet(env,monkeypatch):
  wire,peer=prepare_wire(env,monkeypatch,Sample())
  reports=[]
  with pytest.raises(ConnectionError,match='reconnecting'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:reports.append(k['host_update']['state']))
  assert usb.FINISH in wire.types and startup.MESSAGE in wire.types
  assert usb.registered(env.root,env.value)
  assert 'transferring' in reports
  wire.session.close()


def test_shift_out_of_park_during_upload_never_registers_or_selects(env,monkeypatch):
  sm=Sample(); wire,peer=prepare_wire(env,monkeypatch,sm)
  original=wire.send
  def send(kind,seq,parts,timeout=None):
    original(kind,seq,parts,timeout)
    if wire.types.count(usb.CHUNK)>=2: sm['carState'].gearShifter='drive'
  wire.send=send
  with pytest.raises(ConnectionError,match='Park'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:None)
  assert usb.FINISH not in wire.types and startup.MESSAGE not in wire.types
  assert (env.root/'current').resolve()==env.previous
  wire.session.close()


def test_shift_after_registration_cannot_select_or_activate_new_release(env,monkeypatch):
  sm=Sample(); wire,peer=prepare_wire(env,monkeypatch,sm)
  original=wire.send
  def send(kind,seq,parts,timeout=None):
    original(kind,seq,parts,timeout)
    if kind==usb.FINISH: sm['carState'].gearShifter='drive'
  wire.send=send
  with pytest.raises(ConnectionError,match='Park'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:None)
  assert usb.registered(env.root,env.value)
  assert startup.MESSAGE not in wire.types
  assert (env.root/'current').resolve()==env.previous
  wire.session.close()


def test_waiting_for_park_is_visible_and_cannot_transmit_before_permission(env,monkeypatch):
  sm=Sample(); sm['carState'].gearShifter='drive'
  wire,peer=prepare_wire(env,monkeypatch,sm)
  reports=[]
  def waiting(_):
    assert not wire.types
    sm['carState'].gearShifter='park'
    wire.clock[0]=100.2
  with pytest.raises(ConnectionError,match='reconnecting'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:reports.append(k['host_update']['state']),sleep=waiting)
  assert reports[0]=='waiting_park'
  assert usb.registered(env.root,env.value)
  wire.session.close()


@pytest.mark.parametrize('state,loaded',[('ready','a'*64),('building',None),(None,None)])
def test_onroad_update_requires_explicit_idle_boot_gate(env,monkeypatch,state,loaded):
  wire,peer=prepare_wire(env,monkeypatch,Sample())
  peer.update(engine_state=state,loaded=loaded)
  attached=[True]
  with pytest.raises(ConnectionError,match='offroad'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:attached[0],lambda *a,**k:None,
                                sleep=lambda _:attached.__setitem__(0,False))
  assert not wire.types
  wire.session.close()


def test_normal_runtime_never_enters_usb_update_even_when_onroad_parked():
  startup.wait_for_boot_update(None,{'carrot_host':'jetson','engine_state':'ready'},None,
                              lambda:True,lambda *a,**k:pytest.fail('live runtime update attempted'))


def test_jetson_model_setup_uses_park_permission_but_mac_stays_offroad_only(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import mac
  observed=[]
  spec=mac.JETSON_SPEC
  def ensure(*a,**kw):
    observed.append(kw['should_stop']())
    return spec
  monkeypatch.setattr(mac,'validate_spec',lambda *a:None)
  mac.prepare(NS(ensure_engine=ensure),{'carrot_host':'jetson','loaded':None},lambda:True,lambda:True,lambda *a:None)
  assert observed==[False]
  with pytest.raises(mac.PreparationDeferred):
    mac.prepare(NS(),{'carrot_host':'jetson','loaded':None},lambda:False,lambda:True,lambda *a:None)
  with pytest.raises(mac.PreparationDeferred):
    mac.prepare(NS(),{'protocol':2,'backend':'ort','device':'ane-Apple_M1_Pro'},lambda:False,lambda:True,lambda *a:None)


def test_badge_distinguishes_park_wait_and_actual_usb_transfer():
  from openpilot.common.jetlink_status import update_badge
  assert 'P단' in update_badge({'state':'waiting_park'})[0]
  assert 'USB 전송 중 50%' in update_badge({'state':'transferring','fraction':.5})[0]


def test_first_missing_vehicle_samples_wait_without_recreating_subscriber():
  sm=Sample(); sm.valid['carState']=False
  clock=[100.1]; made=[]; waited=[]
  def factory(): made.append(True); return sm
  permission=policy.Permission(params(),factory=factory,clock=lambda:clock[0])
  def waiting(): waited.append(True)
  def sleep(_): sm.valid['carState']=True; clock[0]=100.2
  policy.wait_for_permission(permission,lambda:True,waiting,sleep)
  assert made==[True] and waited==[True]


def test_permission_wait_aborts_on_disconnect_without_granting_setup():
  attached=[True]
  with pytest.raises(ConnectionError):
    policy.wait_for_permission(NS(allowed=lambda:False),lambda:attached[0],lambda:None,
                               sleep=lambda _:attached.__setitem__(0,False))


def test_bulk_work_leaves_rt_core_and_restores_policy_even_on_failure(monkeypatch):
  monkeypatch.setattr(Path,'is_file',lambda _:True)
  calls=[]
  monkeypatch.setattr(policy.os,'sched_getscheduler',lambda _:1,raising=False)
  monkeypatch.setattr(policy.os,'sched_getparam',lambda _:NS(sched_priority=1),raising=False)
  monkeypatch.setattr(policy.os,'sched_getaffinity',lambda _:{7},raising=False)
  monkeypatch.setattr(policy.os,'sched_setscheduler',lambda *a:calls.append(('policy',a)),raising=False)
  monkeypatch.setattr(policy.os,'sched_setaffinity',lambda *a:calls.append(('cores',a)),raising=False)
  monkeypatch.setattr(policy.os,'sched_param',lambda n:NS(sched_priority=n),raising=False)
  monkeypatch.setattr(policy.os,'SCHED_OTHER',0,raising=False)
  monkeypatch.setattr(policy.os,'cpu_count',lambda:8)
  with pytest.raises(RuntimeError):
    with policy.bulk_scheduling(): raise RuntimeError('synthetic abort')
  assert calls[0][1][1]==0 and calls[1]==('cores',(0,{0,1,2,3}))
  assert calls[-2]==('cores',(0,{7})) and calls[-1][1][1]==1
