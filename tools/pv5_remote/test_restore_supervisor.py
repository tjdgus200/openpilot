import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from tools.pv5_remote import restore_supervisor as s


@unittest.skipUnless(sys.platform == 'linux', 'Linux msgq file semantics')
class BootMsgqChecks(unittest.TestCase):
  def test_prepare_preserves_live_endpoint_and_creates_only_guard_endpoints(self):
    from tools.pv5_remote.conditional_remote import SERVICES
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      live = root / 'msgq_deviceState'
      live.write_bytes(b'live queue data')
      live.chmod(0o600)
      before = live.stat()
      s.prepare_msgq(os.geteuid(), os.getegid(), root)
      s.prepare_msgq(os.geteuid(), os.getegid(), root)
      self.assertEqual({f.name for f in root.iterdir()}, {f'msgq_{name}' for name in SERVICES})
      self.assertEqual(live.read_bytes(), b'live queue data')
      self.assertEqual(live.stat().st_ino, before.st_ino)
      self.assertEqual(live.stat().st_mode, before.st_mode)
      self.assertTrue(all(f.stat().st_uid == os.geteuid() for f in root.iterdir()))
      self.assertTrue(all((f.stat().st_mode & 0o777) == 0o660 for f in root.iterdir() if f != live))

  def test_prepare_refuses_foreign_or_linked_paths_without_modifying_them(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      path = root / 'msgq_deviceState'
      target = root / 'unrelated'
      target.write_bytes(b'untouched')
      for link in (path.symlink_to, path.hardlink_to):
        link(target)
        with self.assertRaisesRegex(ValueError, 'boot_msgq_ownership'):
          s.prepare_msgq(os.geteuid(), os.getegid(), root)
        self.assertEqual(target.read_bytes(), b'untouched')
        path.unlink()
      path.write_bytes(b'foreign queue')
      with self.assertRaisesRegex(ValueError, 'boot_msgq_ownership'):
        s.prepare_msgq(path.stat().st_uid + 1, os.getegid(), root)
      self.assertEqual(path.read_bytes(), b'foreign queue')

  def test_refuses_invalid_launcher_and_nonproduction_namespaces(self):
    with patch.object(s, 'prepare_msgq') as prepare:
      for env in ({}, {'SUDO_UID': '0', 'SUDO_GID': '0'}, {'OPENPILOT_PREFIX': 'test'}):
        with patch.dict(s.os.environ, env, clear=True), self.assertRaisesRegex(ValueError, 'boot_msgq_ownership'):
          s.prepare_launcher_msgq()
      prepare.assert_not_called()


class RestoreSupervisorChecks(unittest.TestCase):
  def setUp(self):
    selected = patch.object(s, 'qualified_interpreter', return_value=sys.executable)
    selected.start()
    self.addCleanup(selected.stop)
    prepare = patch.object(s, 'prepare_launcher_msgq')
    prepare.start()
    self.addCleanup(prepare.stop)

  def run_loop(self, results, configured=None):
    records, sleeps = [], []
    with patch.object(s, 'configured', side_effect=configured or [True] * (len(results) + 1)), \
         patch.object(s, 'attempt_restore', side_effect=results) as attempt, \
         patch.object(s, 'record', side_effect=lambda *x: records.append(x)), \
         patch.object(s.time, 'sleep', side_effect=sleeps.append):
      rc = s.supervise()
    return rc, records, sleeps, attempt.call_count

  def test_transient_timeout_lock_and_startup_then_ready(self):
    result = self.run_loop([('retry', 'attempt_timeout', 'entry'), ('retry', 'install_lock_busy', 'entry'),
                            ('retry', 'unit_not_active_yet', 'runtime_unit_restore'), ('ready', '', 'runtime_unit_restore')])
    self.assertEqual(result[0], 0)
    self.assertEqual(result[2], [2, 4, 8])
    self.assertEqual(result[3], 4)
    self.assertEqual(result[1][-1][0], 'ready')

  def test_backoff_stays_bounded_and_does_not_give_up_on_transient_failures(self):
    rc, records, sleeps, count = self.run_loop([('retry', 'restore_failed', 'entry')] * 12 + [('ready', '', '')])
    self.assertEqual(rc, 0)
    self.assertEqual(count, 13)
    self.assertEqual(sleeps, [2, 4, 8, 16, 32] + [60] * 7)

  def test_revocation_while_waiting_prevents_another_attempt(self):
    rc, records, sleeps, count = self.run_loop([('retry', 'attempt_timeout', 'entry')], [True, False])
    self.assertEqual(count, 1)
    self.assertEqual(records[-1][0], 'disabled')

  def test_identity_mismatch_stops_without_trying_to_requalify(self):
    with patch.object(s, 'configured', side_effect=ValueError('matching_code_qualification_required')), \
         patch.object(s, 'attempt_restore') as attempt, patch.object(s, 'record') as record:
      self.assertEqual(s.supervise(), 1)
      attempt.assert_not_called()
      self.assertEqual(record.call_args.args[:3], ('blocked', 0, 'matching_code_qualification_required'))

  def test_unsafe_msgq_ownership_prevents_starting_the_privileged_guard(self):
    with patch.object(s, 'prepare_launcher_msgq', side_effect=ValueError('boot_msgq_ownership')), \
         patch.object(s, 'run_bounded') as run:
      self.assertEqual(s.attempt_restore(), ('blocked', 'boot_msgq_ownership', 'boot_qualification'))
      run.assert_not_called()

  def test_active_owned_unit_is_checked_without_any_restart_command(self):
    calls = []
    def run(args, timeout):
      calls.append(args)
      return (0, b'{"already_running":true}') if '--restore-runtime' in args else (0, b'active\n')
    with patch.object(s, 'run_bounded', side_effect=run):
      self.assertEqual(s.attempt_restore(), ('ready', '', 'runtime_unit_restore'))
    self.assertEqual(len(calls), 2)
    self.assertFalse(any('restart' in c or 'stop' in c or 'reset-failed' in c for c in calls))

  def test_timeout_retry_and_untrusted_error_output_redaction(self):
    with patch.object(s, 'run_bounded', side_effect=subprocess.TimeoutExpired(['fixture'], 25)):
      self.assertEqual(s.attempt_restore(), ('retry', 'attempt_timeout', 'entry'))
    with patch.object(s, 'run_bounded', return_value=(1, b'{"reason":"SECRET_PATH_KEY","stage":"SECRET"}')):
      self.assertEqual(s.attempt_restore(), ('retry', 'restore_failed', 'entry'))
    with patch.object(s, 'run_bounded', return_value=(1, b'{"reason":"BlockingIOError","errno":11}')):
      self.assertEqual(s.attempt_restore(), ('retry', 'install_lock_busy', 'entry'))

  def test_permanent_rejections_never_retry(self):
    for reason in s.PERMANENT:
      with patch.object(s, 'run_bounded', return_value=(1, json.dumps({'reason': reason}).encode())):
        self.assertEqual(s.attempt_restore(), ('blocked', reason, 'entry'))

  def test_start_request_alone_is_not_marked_ready(self):
    for state in (b'inactive', b'failed', b'activating', b'not-found'):
      with patch.object(s, 'run_bounded', side_effect=[(0, b'{"boot_restore_requested":true}'), (0, state)]):
        self.assertEqual(s.attempt_restore()[0], 'retry')

  def test_config_requires_existing_matching_home_only_qualification(self):
    with tempfile.TemporaryDirectory() as folder, patch.object(s, 'ROOT', Path(folder)), patch.object(s, 'protected'):
      root = Path(folder)
      script = b'public fixture\n'
      (root / 'conditional_remote.py').write_bytes(script)
      base = {'boot_restore': True, 'trial': False, 'qualified_modes': ['detached_home'],
              'resource_policy': 'detached_home_uncapped_v1', 'guard_sha256': hashlib.sha256(script).hexdigest()}
      def config(value):
        (root / 'conditional_modes.json').write_text(json.dumps(value))
      config(base)
      self.assertTrue(s.configured())
      config(dict(base, qualified_modes=['parked_vehicle'], resource_policy='parked_vehicle_capped_v1'))
      self.assertTrue(s.configured())
      config(dict(base, qualified_modes=['detached_home', 'parked_vehicle'], resource_policy='parked_vehicle_capped_v1'))
      with self.assertRaises(ValueError): s.configured()
      config(dict(base, boot_restore=False))
      self.assertFalse(s.configured())
      config(dict(base, trial=True))
      self.assertFalse(s.configured())
      for changes in ({'qualified_modes': ['parked_dashcam']}, {'guard_sha256': '0' * 64}):
        config(dict(base, **changes))
        with self.assertRaises(ValueError):
          s.configured()

  def test_single_status_file_is_replaced_without_raw_log(self):
    with tempfile.TemporaryDirectory() as folder, patch.object(s, 'ROOT', Path(folder)), \
         patch.object(s, 'protected'), patch.object(s.os, 'fchmod', create=True):
      s.record('retry', 1, 'attempt_timeout', 'entry', 25)
      s.record('ready', 2, '', 'runtime_unit_restore', .2)
      files = list(Path(folder).iterdir())
      self.assertEqual([f.name for f in files], ['boot_restore_status.json'])
      data = json.loads(files[0].read_bytes())
      self.assertEqual(data['attempt'], 2)
      self.assertLess(files[0].stat().st_size, 512)

  @unittest.skipUnless(sys.platform == 'linux', 'Linux boot-hook process semantics')
  def test_real_boot_hook_returns_without_waiting_and_releases_boot_fd(self):
    import shlex
    from tools.pv5_remote.conditional_remote import BOOT_HOOK
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      (root / 'TICI').touch()
      (root / 'conditional_modes.json').write_text('{}')
      (root / 'conditional_remote.py').write_text('# fixture')
      helper = root / 'tools/pv5_remote/restore_supervisor.py'
      helper.parent.mkdir(parents=True)
      result = root / 'result'
      helper.write_text('import os,time,json\nfrom pathlib import Path\ntime.sleep(1.5)\ntry:\n os.fstat(9);closed=False\nexcept OSError:\n closed=True\nPath('+repr(str(result))+').write_text(json.dumps({"fd_closed":closed,"env_removed":"CARROT_BOOT_LOCK_FD" not in os.environ}))\n')
      hook = BOOT_HOOK.replace('/TICI', str(root / 'TICI')).replace('/data/pv5-remote-access', str(root))
      script = 'exec 9>'+shlex.quote(str(root / 'lock'))+'\nexport CARROT_BOOT_LOCK_FD=9\nDIR='+shlex.quote(str(root))+'\nsudo() { shift; "$@"; }\n'+hook+'\nprintf returned\n'
      start = time.monotonic()
      run = subprocess.run(['bash', '-c', script], capture_output=True, timeout=2)
      self.assertEqual(run.stdout, b'returned')
      self.assertLess(time.monotonic()-start, 1)
      for _ in range(80):
        if result.exists():
          break
        time.sleep(.05)
      self.assertEqual(json.loads(result.read_text()), {'fd_closed': True, 'env_removed': True})

  @unittest.skipUnless(sys.platform == 'linux', 'Linux process-group semantics')
  def test_actual_slow_attempt_over_old_three_second_budget_succeeds(self):
    start = time.monotonic()
    code, output = s.run_bounded([sys.executable, '-c', 'import time;time.sleep(3.2);print("ready")'], 5)
    self.assertEqual((code, output.strip()), (0, b'ready'))
    self.assertGreater(time.monotonic() - start, 3)

  @unittest.skipUnless(sys.platform == 'linux', 'Linux process-group semantics')
  def test_actual_timeout_cleans_own_descendant_only(self):
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / 'pid'
      # Separate unrelated fixture must survive the timed-out attempt.
      unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'])
      child = 'import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(10)"]);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(10)'
      try:
        with self.assertRaises(subprocess.TimeoutExpired):
          s.run_bounded([sys.executable, '-c', child, str(path)], 1)
        pid = int(path.read_text())
        proc = Path('/proc') / str(pid) / 'stat'
        for _ in range(20):
          if not proc.exists() or proc.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            break
          time.sleep(.05)
        else:
          self.fail('owned child still running')
        self.assertIsNone(unrelated.poll())
      finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)



class QualifiedInterpreterChecks(unittest.TestCase):
  def fixture(self, root):
    script = "def unit_text(interpreter):\n return " + repr('[Service]\nExecStart=') + " + interpreter + " + repr(' ' + str(root / 'conditional_remote.py') + ' --guard\n') + "\n"
    (root/'conditional_remote.py').write_text(script)
    unit = '[Service]\nExecStart=' + sys.executable + ' ' + str(root/'conditional_remote.py') + ' --guard\n'
    (root/s.UNIT).write_text(unit)
    (root/'conditional_modes.json').write_text(json.dumps({'boot_restore':True,'trial':False,'qualified_modes':['detached_home'],
      'resource_policy':'detached_home_uncapped_v1','guard_sha256':hashlib.sha256(script.encode()).hexdigest()}))
    return unit

  @unittest.skipUnless(sys.platform == 'linux', 'Linux absolute interpreter paths')
  def test_registered_interpreter_selected_independently_of_launcher_path(self):
    with tempfile.TemporaryDirectory() as folder, patch.object(s,'ROOT',Path(folder)), patch.object(s,'protected'):
      root=Path(folder);self.fixture(root)
      registered=sys.executable
      self.assertEqual(s.qualified_interpreter(), registered)
      calls=[]
      def run(args, timeout):
        calls.append(args)
        return (0,b'{"already_running":true}') if '--restore-runtime' in args else (0,b'active')
      with patch.object(s.sys,'executable','/different/bootstrap/python3'), patch.object(s,'run_bounded',side_effect=run), \
           patch.object(s, 'prepare_launcher_msgq'):
        self.assertEqual(s.attempt_restore()[0],'ready')
      self.assertEqual(calls[0][0],registered)
      self.assertFalse(any('restart' in args or 'stop' in args for args in calls))

  @unittest.skipUnless(sys.platform == 'linux', 'Linux absolute interpreter paths')
  def test_changed_unit_arguments_and_guard_hash_rejected(self):
    with tempfile.TemporaryDirectory() as folder, patch.object(s,'ROOT',Path(folder)), patch.object(s,'protected'):
      root=Path(folder);unit=self.fixture(root)
      for altered in (unit+'ExecStart=/bin/sh -c unsafe\n', unit.replace(' --guard',' --guard extra'),unit.replace('[Service]','[Service]\nUser=other')):
        (root/s.UNIT).write_text(altered)
        with self.assertRaises(ValueError):s.qualified_interpreter()
      (root/s.UNIT).write_text(unit)
      with (root/'conditional_remote.py').open('a') as f:f.write('# changed')
      with self.assertRaisesRegex(ValueError,'matching_code'):s.qualified_interpreter()


if __name__ == '__main__':
  unittest.main()
