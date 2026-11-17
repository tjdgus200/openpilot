import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tools.pv5_remote import client

class ClientChecks(unittest.TestCase):
  def setUp(self):
    self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
    self.root=Path(self.temp.name);(self.root/'conditional_remote.py').write_bytes(b'enrolled guard')
    self.old=['ssh','-F','none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=private',
              '-o','PasswordAuthentication=no','-o','ServerAliveInterval=2','-o','ServerAliveCountMax=1','root@100.64.0.1']
    self.module={'command':lambda p:list(self.old),'remote':lambda c:'CHECK_ONLY','CHECK':'read-only-check'}
    self.mock=patch.object(client,'connector',return_value=(self.module,self.root));self.mock.start();self.addCleanup(self.mock.stop)
    self.proof={'guard_sha256':hashlib.sha256(b'enrolled guard').hexdigest(),'root_key_session':True,'guard_cgroup':True,
                'host_service_isolation':True,'trial':False,'qualified_modes':['detached_home']}
  def reply(self,code=0,error=b'',proof=None):
    return SimpleNamespace(returncode=code,stderr=error,stdout=json.dumps(self.proof if proof is None else proof).encode())
  def test_options_preserve_key_pin_and_only_replace_liveness_settings(self):
    args=client.command({})
    for value in ('BatchMode=yes','StrictHostKeyChecking=yes','UserKnownHostsFile=private','PasswordAuthentication=no'):
      self.assertIn(value,args)
    for value in ('ConnectionAttempts=3','ServerAliveInterval=5','ServerAliveCountMax=6'):self.assertEqual(args.count(value),1)
    self.assertNotIn('ServerAliveInterval=2',args);self.assertEqual(args[-1],self.old[-1])
  def test_only_fixed_readonly_probe_retries_transient_failure(self):
    replies=iter([self.reply(255,b'connection timed out'),self.reply()]);calls=[];wait=[]
    def runner(args,**kwargs):calls.append(args);return next(replies)
    self.assertEqual(client.check({},runner=runner,sleep=wait.append)['attempts'],2)
    self.assertEqual(len(calls),2);self.assertTrue(all(c[-1]=='CHECK_ONLY' for c in calls));self.assertEqual(wait,[2])
  def test_auth_hostkey_and_remote_error_do_not_retry(self):
    for code,error in ((255,b'Permission denied'),(255,b'Host key verification failed'),(1,b'check failed')):
      with patch.object(client.subprocess,'run') as unused:
        calls=[]
        def runner(*a,**kw):calls.append(1);return self.reply(code,error)
        with self.assertRaises(ValueError):client.check({},runner=runner,sleep=lambda _:self.fail('retry'))
        self.assertEqual(len(calls),1)
  def test_changed_guard_or_isolation_is_not_accepted(self):
    for changes in ({'guard_sha256':'0'*64},{'host_service_isolation':False}):
      with self.assertRaises(ValueError):client.check({},runner=lambda *a,**kw:self.reply(proof=dict(self.proof,**changes)),sleep=lambda _:None)
  def test_timeout_is_bounded_and_reports_unavailable(self):
    calls=[];wait=[]
    def runner(*a,**kw):calls.append(kw['timeout']);raise subprocess.TimeoutExpired('check',25)
    with self.assertRaisesRegex(ValueError,'temporarily_unavailable'):client.check({},runner=runner,sleep=wait.append)
    self.assertEqual(calls,[25]*3);self.assertEqual(wait,[2,4])

if __name__=='__main__':unittest.main()
