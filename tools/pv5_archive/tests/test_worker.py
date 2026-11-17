# The isolated PC harness follows the existing incident unittest suite.
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

from openpilot.system.pv5_incidents.archive_catalog import Catalog
from openpilot.system.pv5_incidents.archive_protocol import encode
from openpilot.system.pv5_incidents.archive_sanitize import PrivacyCatalog, Sanitizer
from openpilot.system.pv5_incidents.archive_worker import Copier, Paused, Transport, configuration, module_identity, share_home_burst
from tools.pv5_archive.store import Store


class WorkerChecks(unittest.TestCase):
  def test_extended_home_transport_wait_cannot_send_after_admission_loss(self):
    for timeout in (6, 30):
      allowed, observed = [True], {}

      class Connection:
        def __init__(self, host, port, **options):
          self.observed, self.allowed = options.pop('observation'), options.pop('admission')
          self.observed.update(options)

        def connect(self):
          self.allowed[0] = False  # Condition changed while TLS was in progress.

        def close(self):
          self.observed['closed'] = True

        def request(self, *args, **kwargs):
          raise AssertionError('must not transmit')

      transport = Transport(urlsplit('https://owner.ts.net:10000/v1/incident'), bytes(32), lambda current=allowed: current[0], timeout=timeout)
      def connection(host, port, current=allowed, observation=observed, **options):
        return Connection(host, port, admission=current, observation=observation, **options)
      with patch('openpilot.system.pv5_incidents.archive_worker.http.client.HTTPSConnection', connection):
        with self.assertRaisesRegex(Paused, 'state'):
          transport.request('/v1/archive/manifest', b'{}')
      self.assertEqual(observed['timeout'], timeout)
      self.assertTrue(observed['closed'])
      self.assertIsNone(transport.connection)

  def test_home_only_budget_exception_keeps_wall_and_permission_limits(self):
    clock, cpu, allowed = [0.0], [0.0], [True]
    regular = Copier(None, SimpleNamespace(), lambda: allowed[0], [], clock=lambda: clock[0], cpu=lambda: cpu[0])
    home = Copier(None, SimpleNamespace(), lambda: allowed[0], [], clock=lambda: clock[0], cpu=lambda: cpu[0], home_unlimited=True)
    cpu[0] = 100
    clock[0] = 1
    with self.assertRaisesRegex(Paused, 'budget'):
      regular.check()
    self.assertTrue(home.check())
    allowed[0] = False
    with self.assertRaisesRegex(Paused, 'state'):
      home.check()
    allowed[0] = True
    clock[0] = 300
    with self.assertRaisesRegex(Paused, 'budget'):
      home.check()

  def test_home_log_and_media_phases_share_deadline_and_cumulative_pacing(self):
    clock = [0.0]
    first = Copier(None, SimpleNamespace(), lambda:True, [], clock=lambda:clock[0], cpu=lambda:0, home_unlimited=True)
    first.bytes, first.segments = 123456, 8
    clock[0] = 250
    second = Copier(None, SimpleNamespace(), lambda:True, [], clock=lambda:clock[0], cpu=lambda:0, home_unlimited=True)
    share_home_burst(second, first.start, first.bytes, first.segments)
    self.assertEqual((second.start, second.bytes, second.segments), (0,123456,8))
    clock[0] = 299
    self.assertTrue(second.check())
    clock[0] = 300
    with self.assertRaisesRegex(Paused, 'budget'):
      second.check()

  def test_actual_copy_resume_chain_and_original_deletion_wins(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      folder = root / 'logs' / '00000341--1810673eb4--14'
      folder.mkdir(parents=True)
      data = b'v' * 150000
      source = folder / 'fcamera.hevc'
      source.write_bytes(data)
      os.utime(source, (1000, 1000))
      allowed = [True]
      raw = Catalog(folder.parent, root / 'catalog.sqlite', bytes(32), lambda: allowed[0], clock=lambda: 2000)
      safe = PrivacyCatalog(raw, Sanitizer(root / 'cache', raw.check))
      store = Store(root / 'pc', bytes(32), reserve=0)

      class Local:
        before_send = None
        calls = 0

        def request(self, path, body):
          self.before_send()
          if path.endswith('manifest'):
            return store.begin(body)
          self.calls += 1
          if '/chunk/' in path:
            _, _, _, _, token, offset = path.split('/')
            result = store.chunk(token, int(offset), body)
            if self.calls == 1:
              raise Paused('network')  # Server committed, client lost ACK.
            return result
          return store.seal(path.rsplit('/', 1)[1], body)

        def close(self):
          pass

      transport = Local()
      copier = Copier(safe, transport, lambda: allowed[0], ['trusted'], profile=lambda: 'trusted', rate=10**9, cpu=lambda: 0)
      with self.assertRaises(Paused):
        copier.run('a' * 40)
      result = Copier(safe, transport, lambda: allowed[0], ['trusted'], profile=lambda: 'trusted', rate=10**9, cpu=lambda: 0).run('a' * 40)
      self.assertEqual(result['segments'], 1)
      job = store.claim()
      self.assertEqual(Path(job['files']['fcamera.hevc']).read_bytes(), data)
      self.assertEqual(source.read_bytes(), data)
      self.assertIsNone(raw.next('a' * 40))

  def test_untrusted_profile_and_state_stop_before_request(self):
    class Local:
      def close(self):
        pass

    for allowed, profile in ((False, 'trusted'), (True, 'hotspot')):
      copier = Copier(None, Local(), lambda value=allowed: value, ['trusted'], profile=lambda value=profile: value, cpu=lambda: 0)
      with self.assertRaises(Paused):
        copier.run('a' * 40)
    for endpoint in ('http://private.ts.net:10000/v1/incident', 'https://evil.example:10000/v1/incident'):
      with self.assertRaises(ValueError):
        Transport(urlsplit(endpoint), bytes(32), lambda: True)

  def test_private_config_default_off_and_code_identity(self):
    with tempfile.TemporaryDirectory() as temporary:
      path = Path(temporary) / 'config.json'
      value = {
        'version': 1,
        'vehicle': 'KIA_PV5',
        'enabled': True,
        'qualified_modes': ['detached_offroad'],
        'wifi_profiles': ['a' * 64],
        'module_identity': module_identity(),
      }
      path.write_bytes(encode(value))
      path.chmod(0o600)
      self.assertEqual(configuration(path)['qualified_modes'], ['detached_offroad'])
      for key, bad in (('enabled', False), ('module_identity', '0' * 64), ('qualified_modes', []), ('vehicle', 'OTHER')):
        path.write_bytes(encode(dict(value, **{key: bad})))
        with self.assertRaises(ValueError):
          configuration(path)

  def test_permission_eof_kills_only_dedicated_helper_group(self):
    with tempfile.TemporaryDirectory() as temporary:
      pidfile = Path(temporary) / 'pids.json'
      code = '; '.join(
        [
          'from openpilot.system.pv5_incidents.archive_worker import Permission',
          'import subprocess,sys,time,os,json',
          'p=Permission(0)',
          "c=subprocess.Popen(['/bin/sleep','60'])",
          "open(sys.argv[1],'w').write(json.dumps([os.getpid(),c.pid]))",
          'time.sleep(60)',
        ]
      )
      child = subprocess.Popen([sys.executable, '-c', code, str(pidfile)], stdin=subprocess.PIPE, start_new_session=True)
      try:
        for _ in range(40):
          if pidfile.exists():
            break
          time.sleep(0.025)
        pids = json.loads(pidfile.read_text())
        child.stdin.close()
        child.wait(timeout=2)
        for _ in range(40):
          alive = [
            pid for pid in pids if Path('/proc', str(pid), 'stat').exists() and Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
          ]
          if not alive:
            break
          time.sleep(0.025)
        self.assertEqual(alive, [])
      finally:
        if child.poll() is None:
          os.killpg(child.pid, 9)
          child.wait()


if __name__ == '__main__':
  unittest.main()


class OffloadConfiguration(unittest.TestCase):
  def test_offload_is_explicit_home_only_and_identity_covers_new_modules(self):
    from openpilot.system.pv5_incidents.archive_worker import MODULES
    self.assertIn('archive_parallel.py', MODULES)
    self.assertIn('archive_offload.py', MODULES)
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp)/'config.json'
      value = {'version':1, 'vehicle':'KIA_PV5', 'enabled':True, 'qualified_modes':['detached_offroad'],
               'wifi_profiles':['a'*64], 'module_identity':module_identity()}
      def save():
        path.write_bytes(encode(value))
        path.chmod(0o600)
      save()
      self.assertNotIn('pc_preprocess', configuration(path))
      value['pc_preprocess'] = True
      save()
      self.assertTrue(configuration(path)['pc_preprocess'])
      value['qualified_modes'] = ['detached_offroad', 'dashcam_stationary']
      save()
      with self.assertRaisesRegex(ValueError, 'home_only'):
        configuration(path)
