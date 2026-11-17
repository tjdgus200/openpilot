"""PC-only stop-gate and closed-recording event transfer tests."""
import os
from types import SimpleNamespace as NS

import pytest
from openpilot.cereal import log
from openpilot.system.pv5_incidents.archive_catalog import Catalog, SourceChanged
from openpilot.system.pv5_incidents.archive_worker import Copier, Paused
from openpilot.system.pv5_incidents.archive_sanitize import PrivacyCatalog, Sanitizer
from openpilot.system.pv5_incidents.stationary_event_gate import evaluate, Admission, MODE
from openpilot.system.pv5_incidents.stationary_event_catalog import Evidence, EventCatalog
from tools.pv5_archive.tests.test_gate import SM
from tools.pv5_archive.store import Store


def stopped():
  sm = SM()
  sm['deviceState'].started = True
  sm.data['roadEncodeIdx'] = NS(segmentNum=1)
  sm['carParams'].passive = False
  sm['carParams'].dashcamOnly = False
  return sm


def car(t, accel=0., speed=10., aeb=False, valid=True):
  e = log.Event.new_message(); e.logMonoTime = int(t*1e9); e.valid = valid
  cs = e.init('carState'); cs.canValid = True; cs.vEgo = speed; cs.aEgo = accel; cs.stockAeb = aeb
  return e


def output(t, raw=-3.2, age=.05, version=1):
  e = log.Event.new_message(); e.logMonoTime = int(t*1e9); e.valid = True
  a = e.init('carOutput').actuatorsOutput
  a.pv5SccWarningVersion = version; a.pv5SccWarningRaw = raw
  a.pv5SccWarningMonoTime = a.pv5SccDecelSourceMonoTime = int((t-age)*1e9)
  return e


def test_new_mode_requires_own_qualification_and_two_second_admission():
  sm = stopped()
  assert not evaluate(sm, 10, ['detached_offroad', 'dashcam_stationary'], True).allowed
  gate = Admission()
  for i in range(20):
    assert not gate.tick(evaluate(sm, 10, [MODE], True), i/10)
  assert gate.tick(evaluate(sm, 10, [MODE], True), 2.)
  sm['carState'].cruiseState.enabled = True
  assert not gate.tick(evaluate(sm, 10, [MODE], True), 2.1)


@pytest.mark.parametrize('topic,field,value', [
  ('carState','vEgoRaw',.01),('carState','vEgo',.02),('carState','vEgo',float('nan')),
  ('carState','standstill',False),('carState','canValid',False),('carState','canTimeout',True),('carState','stockAeb',True),
  ('carControl','enabled',True),('carControl','latActive',True),('carControl','longActive',True),
  ('selfdriveState','enabled',True),('selfdriveState','active',True),('selfdriveState','state','preEnabled'),
  ('selfdriveState','alertStatus','critical'),('deviceState','networkType','cell4G'),('deviceState','networkMetered',True),
  ('deviceState','cpuUsagePercent',[71]),('deviceState','memoryUsagePercent',81),('deviceState','freeSpacePercent',19),
  ('deviceState','started',False),('carParams','notCar',True),('carParams','carFingerprint','OTHER')])
def test_admission_rejects_motion_control_resource_and_identity(topic, field, value):
  sm = stopped(); setattr(sm[topic],field,value)
  assert not evaluate(sm,10,[MODE],True).allowed


@pytest.mark.parametrize('service', ['carState','carControl','selfdriveState','pandaStates','deviceState','managerState','onroadEvents'])
def test_stale_state_cannot_authorize_work(service):
  sm=stopped(); sm.logMonoTime[service]=0
  assert not evaluate(sm,10,[MODE],True).allowed


def test_emergency_preempts_upload():
  sm=stopped();sm.data['onroadEvents']=[NS(immediateDisable=True,softDisable=False)]
  assert not evaluate(sm,10,[MODE],True).allowed


@pytest.mark.parametrize('accel,speed,expected', [(-3.1,10,True),(-2.9,10,False),(-3.1,1,False),(float('nan'),10,False)])
def test_hard_decel_is_persistent_and_not_small_noise(accel,speed,expected):
  evidence=Evidence()
  assert evidence.observe(car(10,accel,speed)) is None
  assert evidence.observe(car(10.1,accel,speed)) is None
  assert (evidence.observe(car(10.21,accel,speed))=='hard_deceleration') is expected


def test_aeb_and_strong_oem_request_are_separate_candidates():
  evidence=Evidence()
  assert evidence.observe(car(10,aeb=True))=='recorded_stock_aeb'
  assert evidence.observe(output(10.1))=='stock_scc_hard_request'
  assert evidence.observe(output(10.1,raw=-.5)) is None
  assert evidence.observe(output(10.1,age=.16)) is None
  assert evidence.observe(output(10.1,version=0)) is None
  assert evidence.observe(output(11)) is None


def make_segment(root, n, events, locked=False):
  folder=root/f'00000341--1810673eb4--{n}';folder.mkdir()
  (folder/'rlog').write_bytes(b''.join(e.to_bytes() for e in events))
  (folder/'fcamera.hevc').write_bytes(b'road-test-fixture'*100)
  (folder/'dcamera.hevc').write_bytes(b'private-cabin')
  (folder/'audio.wav').write_bytes(b'private-audio')
  for path in folder.iterdir():os.utime(path,(1000,1000))
  if locked:(folder/'rlog.lock').touch()
  return folder


def catalogs(tmp_path, events=None):
  logs=tmp_path/'logs';logs.mkdir()
  for n in range(5):
    make_segment(logs,n,events if n==2 and events else [car(10+n)])
  allowed=[True]
  cat=EventCatalog(logs,tmp_path/'archive.sqlite',bytes(32),lambda:allowed[0],clock=lambda:2000)
  return cat,allowed


def test_normal_logs_do_not_upload_and_do_not_hide_home_backlog(tmp_path):
  cat,_=catalogs(tmp_path)
  assert cat.next('a'*40) is None
  ordinary=Catalog(cat.root,cat.database,bytes(32),lambda:True,clock=lambda:2000)
  assert ordinary.next('a'*40) is not None


def test_event_context_durable_resume_and_closed_file_requirements(tmp_path):
  cat,allowed=catalogs(tmp_path,[car(20,aeb=True)])
  # Neighbor currently recording is retained for later, never forced closed.
  lock=cat.root/'00000341--1810673eb4--3'/'rlog.lock';lock.touch()
  names=[]
  while item:=cat.next('a'*40):
    names.append(item['manifest']['segment']);cat.completed(item)
  assert [int(x.rsplit('--',1)[1]) for x in names]==[1,2]
  lock.unlink()
  resumed=EventCatalog(cat.root,cat.database,bytes(32),lambda:allowed[0],clock=lambda:2000)
  item=resumed.next('a'*40)
  assert item['manifest']['segment'].endswith('--3')
  assert {f['name'] for f in item['manifest']['files']}=={'rlog','fcamera.hevc'}
  allowed[0]=False
  with pytest.raises(SourceChanged):resumed.read(item,item['manifest']['files'][0],0,64)
  with pytest.raises(SourceChanged):resumed.completed(item)


def test_prepare_transfer_seal_and_resume_use_existing_pc_protocol(tmp_path):
  cat,allowed=catalogs(tmp_path,[car(20),output(20.1)])
  originals={str(p):p.read_bytes() for p in cat.root.rglob('*') if p.is_file()}
  store=Store(tmp_path/'pc',bytes(32),reserve=0)
  class Local:
    before_send=None
    manifests=[]
    def request(self,path,body):
      self.before_send()
      if path.endswith('manifest'):
        import json
        self.manifests.append(json.loads(body))
        return store.begin(body)
      if '/chunk/' in path:
        return store.chunk(path.split('/')[-2],int(path.split('/')[-1]),body)
      return store.seal(path.rsplit('/',1)[1],body)
    def close(self):pass
  transport=Local()
  copier=Copier(cat,transport,lambda:allowed[0],['trusted'],profile=lambda:'trusted',rate=10**9,cpu=lambda:0)
  cat.budget_check=copier.check
  copier.catalog=PrivacyCatalog(cat,Sanitizer(tmp_path/'cache',copier.check))
  result=copier.run('a'*40)
  assert result['segments']==3
  assert all({f['name'] for f in m['files']}=={'rlog.zst','fcamera.hevc'} for m in transport.manifests)
  assert copier.run('a'*40)['segments']==3  # Already completed, no new manifest.
  assert len(transport.manifests)==3
  assert all(p.read_bytes()==originals[str(p)] for p in cat.root.rglob('*') if p.is_file())


def test_pc_only_strong_screened_events_wake_analysis_with_existing_cooldown(tmp_path):
  from unittest.mock import Mock
  from openpilot.system.pv5_incidents.priority import archive_reviewable, fault_key
  from tools.pv5_archive.dispatch import ArchiveDispatch
  summary={'screening_policy':'risk-first-v1','status':'review_candidates','source_commit':'a'*40,
           'error_codes':['locationdTemporaryError'], 'missing_services':[],
           'candidates':[{'kind':'stock_scc_hard_request'}],
           'review_windows':[{'start_seconds':1,'end_seconds':11,'reasons':['stock_scc_hard_request']}]}
  assert archive_reviewable(summary)
  assert fault_key(summary) is None  # Previous same-version fault must not hide a new driving event.
  for kind in ('assistance','radar_value_jump','recorded_system_disable'):
    ordinary={**summary,'error_codes':[],'candidates':[{'kind':kind}], 'review_windows':[{'reasons':[kind]}]}
    assert not archive_reviewable(ordinary)
  store=Store(tmp_path/'pc',bytes(32),reserve=0);send=Mock(return_value={'status':'accepted'});clock=[10000.]
  dispatch=ArchiveDispatch(store,tmp_path/'events',lambda:{'enabled':True,'push_policy':'analysis_only'},send,lambda:clock[0])
  store.analyzed('a'*64,summary);dispatch.step();assert send.call_count==1
  store.analyzed('b'*64,summary);clock[0]+=301;dispatch.step();assert send.call_count==1
  clock[0]+=1500;dispatch.step();assert send.call_count==2


def test_recent_scan_and_loss_before_preparation_do_no_io(tmp_path):
  cat,allowed=catalogs(tmp_path)
  assert cat.scan.next('a'*40)['manifest']['segment'].endswith('--4')
  allowed[0]=False
  with pytest.raises(SourceChanged):cat.next('a'*40)
  with cat.scan.db() as db:assert db.execute('SELECT count(*) FROM completed').fetchone()[0]==0


def test_invalid_car_record_cannot_support_oem_request():
  evidence=Evidence();evidence.observe(car(10));evidence.observe(car(10.05,valid=False))
  assert evidence.observe(output(10.1)) is None


def test_new_coordinator_closes_only_owned_worker_on_cruise_activation(tmp_path,monkeypatch):
  import json,subprocess,sys,time
  from openpilot.system.pv5_incidents import stationary_event_runtime as runtime
  pids=tmp_path/'pids.json';script=tmp_path/'child.py'
  script.write_text("import json,subprocess,os,time\nc=subprocess.Popen(['/bin/sleep','60'])\nopen("+repr(str(pids))+",'w').write(json.dumps([os.getpid(),c.pid]))\ntime.sleep(60)\n")
  original=subprocess.Popen
  monkeypatch.setattr(runtime.subprocess,'Popen',lambda *args,**kw:original([sys.executable,str(script)],**kw))
  co=runtime.Coordinator({'qualified_modes':[MODE],'vehicle':'KIA_PV5'},'/unused','/unused','a'*40)
  try:
    for now in (10,10.5,11,11.5,12):
      sm=stopped();sm.logMonoTime={k:int(now*1e9) for k in sm.logMonoTime};co.tick(sm,now)
    for _ in range(40):
      if pids.exists():break
      time.sleep(.025)
    owned=json.loads(pids.read_text())
    sm=stopped();sm.logMonoTime={k:int(12.1e9) for k in sm.logMonoTime};sm['carState'].cruiseState.enabled=True
    co.tick(sm,12.1);assert co.child is None
    from pathlib import Path
    for _ in range(80):
      alive=[pid for pid in owned if Path(f'/proc/{pid}/stat').exists() and Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]!='Z']
      if not alive:break
      time.sleep(.025)
    assert not alive
  finally:
    co.cancel()


def test_event_config_cannot_reuse_home_identity_or_modes(tmp_path):
  import json
  from openpilot.system.pv5_incidents.stationary_event_gate import configuration,module_identity
  from openpilot.system.pv5_incidents.archive_worker import module_identity as home_identity
  path=tmp_path/'events.json'
  cfg={'version':1,'vehicle':'KIA_PV5','enabled':True,'qualified_modes':[MODE],
       'wifi_profiles':['a'*64],'module_identity':module_identity()}
  path.write_text(json.dumps(cfg));path.chmod(0o600)
  assert configuration(path)==cfg
  for field,value in [('qualified_modes',['detached_offroad']),('module_identity',home_identity()),('enabled',False)]:
    path.write_text(json.dumps({**cfg,field:value}))
    with pytest.raises(ValueError):configuration(path)
  path.write_text(json.dumps(cfg));path.chmod(0o644)
  with pytest.raises(ValueError):configuration(path)


def test_locked_event_source_is_never_scanned_or_sent(tmp_path):
  cat,_=catalogs(tmp_path,[car(20,aeb=True)])
  (cat.root/'00000341--1810673eb4--2'/'rlog.lock').touch()
  assert cat.next('a'*40) is None
  with cat.scan.db() as db:assert db.execute('SELECT count(*) FROM selected').fetchone()[0]==0
