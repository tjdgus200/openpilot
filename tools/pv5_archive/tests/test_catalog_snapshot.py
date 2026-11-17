import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from openpilot.system.pv5_incidents.archive_catalog import Catalog
from tools.pv5_archive.tests.test_store import KEY

class CatalogSnapshotChecks(unittest.TestCase):
  def test_completed_backlog_needs_one_database_open_and_current_identity(self):
    with tempfile.TemporaryDirectory() as tmp:
      r=Path(tmp);logs=r/'logs';logs.mkdir();cat=Catalog(logs,r/'catalog.sqlite',KEY,lambda:True,clock=lambda:2000)
      for i in range(50):
        folder=logs/('route--'+str(i));folder.mkdir();f=folder/'rlog';f.write_bytes(b'fixture');os.utime(f,(1000,1000))
        st=folder.stat()
        with cat.db() as db:db.execute('INSERT INTO completed VALUES(?,?,?)',(folder.name,st.st_ino,st.st_mtime_ns))
      with patch.object(cat,'db',wraps=cat.db) as opens:
        self.assertIsNone(cat.next('a'*40));self.assertEqual(opens.call_count,1)
      (logs/'route--0'/'other').touch()
      self.assertEqual(cat.next('a'*40)['manifest']['segment'],'route--0')
      self.assertIsNone(cat.next('a'*40,exclude={'route--0'}))
