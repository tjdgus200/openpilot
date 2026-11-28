"""Park mode is explicit; actual limits and host-bound cancellation are mandatory."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from tools.pv5_remote import conditional_remote as r
from tools.pv5_remote import connect, pc
from tools.pv5_remote.test_remote import home, parked, key
from openpilot.system.pv5_incidents.archive_protocol import encode, headers


class ParkChecks(unittest.TestCase):
  def config(self):
    return {'qualified_modes': r.PARK_MODES, 'resource_policy': r.PARK_POLICY}

  def test_normal_park_is_explicit_and_existing_home_is_separate(self):
    sample = parked()
    sample['carParams']['dashcam'] = False
    self.assertEqual(r.eligible(sample, 10, r.PARK_MODES), ('parked_vehicle', ''))
    self.assertFalse(r.eligible(sample, 10, ['parked_dashcam'])[0])
    self.assertFalse(r.eligible(sample, 10, ['detached_home'])[0])
    self.assertFalse(r.eligible(home(), 10, r.PARK_MODES)[0])
    self.assertEqual(r.access_policy(self.config()), r.PARK_POLICY)
    for cfg in ({'resource_policy': r.HOME_POLICY, 'qualified_modes': r.PARK_MODES},
                {'resource_policy': r.PARK_POLICY, 'qualified_modes': ['parked_dashcam']}):
      with self.assertRaises(ValueError): r.access_policy(cfg)

  def test_exact_zero_and_every_control_requires_false(self):
    for section, fields in (('carState', ('cruise','aeb')), ('carControl', ('enabled','lat','long')),
                           ('selfdriveState', ('enabled','active','critical')), ('onroadEvents', ('disable',))):
      for field in fields:
        for value in (True, None, 0, ''):
          s=parked();s[section][field]=value
          self.assertFalse(r.eligible(s,10,r.PARK_MODES)[0],(section,field,value))
    for field in ('raw_speed','speed'):
      for value in (0.00001,-0.00001,float('nan'),None):
        s=parked();s['carState'][field]=value
        self.assertFalse(r.eligible(s,10,r.PARK_MODES)[0])
    for field, value in (('gear','drive'),('can_valid',False),('can_timeout',True),('standstill',False)):
      s=parked();s['carState'][field]=value
      self.assertFalse(r.eligible(s,10,r.PARK_MODES)[0])

  def test_freshness_and_overload_close_without_waiting_for_15_minutes(self):
    g=r.Gate()
    for i in range(21): action=g.tick('parked_vehicle',i/10)
    self.assertEqual(action,'open')
    for i in range(1,10001): self.assertEqual(g.tick('parked_vehicle',2+i/10),'keep')
    self.assertEqual(g.tick('',1002.1),'close')
    for name in ('deviceState','pandaStates','managerState','carState','carControl','selfdriveState','onroadEvents'):
      s=parked();s[name]['valid']=False
      self.assertFalse(r.eligible(s,10,r.PARK_MODES)[0])
    for field,value in (('cpu',[71]),('memory',81),('free',19),('thermal',1)):
      s=parked();s['deviceState'][field]=value
      self.assertFalse(r.eligible(s,10,r.PARK_MODES)[0])
    g=r.Gate()
    for i in range(21): action=g.tick('parked_vehicle',i/10)
    self.assertEqual(g.tick('detached_home',2.1),'close')

  def test_local_park_wait_covers_car_params_publish_interval(self):
    from types import SimpleNamespace
    now = 0.

    def pause(seconds):
      nonlocal now
      now += seconds

    def read():
      sample = parked()
      for row in sample.values():
        row['stamp'] = now
      sample['carParams']['valid'] = now >= 50
      return sample

    source = SimpleNamespace(read=read)
    self.assertEqual(r.await_eligible(source, r.PARK_MODES, clock=lambda: now, pause=pause), 'parked_vehicle')
    self.assertGreaterEqual(now, 52)

  def fixture(self, root, memory='268435456', cpu='10000 100000'):
    proc=root/'proc';proc.write_text('0::/system.slice/'+r.UNIT+'\n')
    base=root/'sys/system.slice'/r.UNIT;base.mkdir(parents=True)
    (base/'memory.max').write_text(memory)
    (base/'pids.max').write_text('64')
    if cpu is not None: (base/'cpu.max').write_text(cpu)
    return proc,base

  def test_kernel_limits_required_not_just_directives(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);proc,base=self.fixture(root)
      proof=r.resource_limits(self.config(),proc,root/'sys')
      self.assertEqual((proof['memory_limit'],proof['CPU_quota_limit'],proof['tasks_limit']), (268435456,.1,64))
      (base/'pids.max').write_text('max')
      with self.assertRaisesRegex(ValueError, 'vehicle_tasks_limit_required'):
        r.resource_limits(self.config(),proc,root/'sys')
      (base/'pids.max').write_text('64')
      for value in ('max 100000','-1 100000','10001 100000','0 100000'):
        (base/'cpu.max').write_text(value)
        with self.assertRaises(ValueError): r.resource_limits(self.config(),proc,root/'sys')
      (base/'cpu.max').write_text('10000 100000')
      for value in ('max','268435457','0'):
        (base/'memory.max').write_text(value)
        with self.assertRaises(ValueError): r.resource_limits(self.config(),proc,root/'sys')
      (base/'memory.max').write_text('268435456')
      (base/'cpu.max').unlink()
      with self.assertRaises(ValueError): r.resource_limits(self.config(),proc,root/'sys')
      proc.write_text('0::/user.slice/foreign\n')
      with self.assertRaises(ValueError): r.resource_limits(self.config(),proc,root/'sys')

  def test_existing_hybrid_cpu_controller_same_cgroup_only(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);proc,base=self.fixture(root,cpu=None)
      group='/system.slice/'+r.UNIT
      proc.write_text('0::'+group+'\n3:cpu,cpuacct:'+group+'\n')
      cpu=root/'sys/cpu,cpuacct'/group.lstrip('/');cpu.mkdir(parents=True)
      (cpu/'cpu.cfs_quota_us').write_text('10000');(cpu/'cpu.cfs_period_us').write_text('100000')
      self.assertEqual(r.resource_limits(self.config(),proc,root/'sys')['CPU_controller'],'v1')
      proc.write_text('0::'+group+'\n3:cpu,cpuacct:/user.slice/other\n')
      with self.assertRaises(ValueError): r.resource_limits(self.config(),proc,root/'sys')

  def test_house_uncapped_not_silently_mixed_and_unit_has_no_expiry(self):
    cfg={'qualified_modes':['detached_home'],'resource_policy':r.HOME_POLICY}
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);proc,base=self.fixture(root,memory='max',cpu='max 100000')
      self.assertIsNone(r.resource_limits(cfg,proc,root/'sys')['memory_limit'])
      with self.assertRaises(ValueError): r.resource_limits(self.config(),proc,root/'sys')
    unit=r.unit_text('/usr/local/venv/bin/python3',r.PARK_POLICY)
    for item in ('CPUQuota=10%','MemoryMax=256M','TasksMax=64','ProtectControlGroups=yes','WatchdogSec=1s','KillMode=control-group','Restart=always'):
      self.assertIn(item,unit)
    self.assertNotIn('RuntimeMaxSec=',unit)
    self.assertNotIn('CPUQuota=',r.unit_text('/usr/local/venv/bin/python3'))
    compile(connect.CHECK_PARK,'park-check','exec')
    self.assertIn("==['parked_vehicle']", connect.CHECK_PARK)
    self.assertNotIn("==['detached_home']", connect.CHECK_PARK)
    self.assertIn("assert admitted=='parked_vehicle'",connect.CHECK_PARK)

  def test_old_home_attestation_cannot_qualify_park(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);root.joinpath('client_ed25519.pub').write_text(key(1))
      b=pc.Bootstrap(root,bytes(range(32)),enabled=lambda:True)
      body=encode({'version':1,'host_key':key(2)})
      def call(path, nonce): return b.ingest(path,headers(bytes(range(32)),path,body,1000,nonce),body,1000)
      call(pc.PATH,'a'*32)
      proof={'passed':True,'host_key':key(2),'stamp':999,'resource_policy':r.HOME_POLICY,'guard_sha256':'a'*64}
      root.joinpath('home-qualification.json').write_text(json.dumps(proof))
      with self.assertRaises(ValueError):call(pc.PARK_QUALIFICATION,'b'*32)
      proof.update(resource_policy=r.PARK_POLICY,qualified_modes=r.PARK_MODES,admitted_mode='parked_vehicle',transport_closed=True,memory_limit=268435456,CPU_quota_limit=.1,tasks_limit=64)
      root.joinpath('park-qualification.json').write_text(json.dumps(proof))
      self.assertTrue(call(pc.PARK_QUALIFICATION,'c'*32)['park_qualified'])
      proof['admitted_mode']='detached_home'
      root.joinpath('park-qualification.json').write_text(json.dumps(proof))
      with self.assertRaises(ValueError):call(pc.PARK_QUALIFICATION,'d'*32)

  @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'requires no-follow file opens')
  def test_park_menu_preference_accepts_only_exact_one_in_regular_file(self):
    from openpilot.common import pv5_park_ssh

    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / 'setting'
      self.assertFalse(pv5_park_ssh.requested(path))
      for value in (b'0', b'', b'1\\n', b'10'):
        path.write_bytes(value)
        self.assertFalse(pv5_park_ssh.requested(path))
      path.write_bytes(b'1')
      self.assertTrue(pv5_park_ssh.requested(path))
      link = path.with_name('link')
      link.symlink_to(path)
      self.assertFalse(pv5_park_ssh.requested(link))

  @unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root-owned runtime marker')
  def test_menu_recognizes_either_qualified_service(self):
    from unittest.mock import patch
    from openpilot.common import pv5_park_ssh as menu

    with tempfile.TemporaryDirectory() as directory:
      home = Path(directory) / 'home-marker'
      park = Path(directory) / 'park-marker'
      with patch.object(menu, 'MARKER', home), patch.object(menu, 'PARK_MARKER', park):
        self.assertFalse(menu.qualified())
        for marker in (home, park):
          marker.touch(mode=0o644)
          self.assertTrue(menu.qualified())
          marker.chmod(0o666)
          self.assertFalse(menu.qualified())
          marker.unlink()
        home.touch(mode=0o644)
        park.symlink_to(home)
        self.assertFalse(menu.qualified(park))


if __name__ == '__main__': unittest.main()
