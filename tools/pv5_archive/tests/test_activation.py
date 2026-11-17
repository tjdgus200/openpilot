import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openpilot.system.pv5_incidents.archive_worker import configuration, module_identity
from openpilot.system.pv5_incidents.archive_runtime import Coordinator

class ActivationChecks(unittest.TestCase):
  def test_temporary_config_has_expiry_and_requires_home_offload(self):
    with tempfile.TemporaryDirectory() as directory:
      p = Path(directory) / 'config.json'
      base = dict(version=1, vehicle='KIA_PV5', enabled=True, qualified_modes=['detached_offroad'],
                  wifi_profiles=['a'*64], module_identity=module_identity(), pc_preprocess=True,
                  qualification_expires_at=1100)
      def read(value):
        p.write_text(json.dumps(value));p.chmod(0o600)
        with patch('openpilot.system.pv5_incidents.archive_worker.time.time', return_value=1000):
          return configuration(p)
      self.assertEqual(read(base), base)
      for expires in (True,1000,1601,float('inf'),'1100'):
        with self.assertRaises(ValueError):read(dict(base,qualification_expires_at=expires))
      for changes in ({'pc_preprocess':False},{'qualified_modes':['dashcam_stationary']},{'module_identity':'0'*64}):
        with self.assertRaises(ValueError):read(dict(base,**changes))
      permanent = dict(base);permanent.pop('qualification_expires_at')
      self.assertEqual(read(permanent),permanent)

  def test_trial_expiry_cancels_without_reading_or_relaxing_gate(self):
    with patch('openpilot.system.pv5_incidents.archive_runtime.time.time',return_value=1000):
      co=Coordinator({'qualification_expires_at':1100},'/unused','/unused','a'*40,clock=lambda:10)
    for wall,mono in ((1100,20),(900,110)):
      with patch('openpilot.system.pv5_incidents.archive_runtime.time.time',return_value=wall),patch.object(co,'cancel') as cancel:
        co.tick(None,mono)
        cancel.assert_called_once()
        self.assertEqual(co.last_reason,'qualification_expired')

if __name__=='__main__':unittest.main()
