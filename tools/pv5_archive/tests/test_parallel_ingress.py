from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch
from openpilot.system.pv5_incidents.archive_protocol import BATCH,ZERO_CHAIN,advance,encode,CHUNK
from tools.pv5_archive.store import Store
from tools.pv5_archive.tests.test_store import KEY,description

class ParallelIngressChecks(unittest.TestCase):
  def test_independent_files_fsync_overlap_and_seal_exact_bytes(self):
    with tempfile.TemporaryDirectory() as tmp:
      store=Store(Path(tmp),KEY,reserve=0)
      entries=[];stripes=set()
      for i in range(100):
        value=description({'rlog.zst':b'x'*BATCH},segment='parallel--'+str(i))
        entry=value['files'][0];stripe=int(entry['file_id'][:8],16)%64
        if stripe in stripes:continue
        stripes.add(stripe);store.begin(encode(value));entries.append(entry)
        if len(entries)==4:break
      state={'active':0,'maximum':0};lock=threading.Lock();barrier=threading.Barrier(4)
      import os
      fsync=os.fsync
      def sync(fd):
        with lock:state['active']+=1;state['maximum']=max(state['maximum'],state['active'])
        try:barrier.wait(timeout=3);fsync(fd)
        finally:
          with lock:state['active']-=1
      with patch('tools.pv5_archive.store.os.fsync',side_effect=sync),ThreadPoolExecutor(4) as pool:
        results=list(pool.map(lambda e:store.batch(e['file_id'],0,b'x'*BATCH),entries))
      self.assertEqual(state['maximum'],4)
      with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda er:store.seal(er[0]['file_id'],encode({'size':BATCH,'chain':er[1]['chain']})),zip(entries,results)))
      for _ in entries:
        job=store.claim();self.assertEqual(Path(job['files']['rlog.zst']).read_bytes(),b'x'*BATCH)
      self.assertIsNone(store.claim())

  def test_same_file_duplicates_have_one_durable_write(self):
    with tempfile.TemporaryDirectory() as tmp:
      store=Store(Path(tmp),KEY,reserve=0);v=description({'rlog.zst':b'x'*BATCH});store.begin(encode(v));token=v['files'][0]['file_id']
      import os
      fsync=os.fsync
      with patch('tools.pv5_archive.store.os.fsync',wraps=fsync) as sync,ThreadPoolExecutor(4) as pool:
        results=list(pool.map(lambda _:store.batch(token,0,b'x'*BATCH),range(4)))
      self.assertEqual(sync.call_count,1);self.assertTrue(all(x==results[0] for x in results))
      store.seal(token,encode({'size':BATCH,'chain':results[0]['chain']}))
      self.assertEqual(Path(store.claim()['files']['rlog.zst']).stat().st_size,BATCH)

  def test_failed_durable_write_does_not_advance_other_file_or_fake_completion(self):
    with tempfile.TemporaryDirectory() as tmp:
      store=Store(Path(tmp),KEY,reserve=0);v=description({'rlog.zst':b'x'*CHUNK});store.begin(encode(v));token=v['files'][0]['file_id']
      with patch('tools.pv5_archive.store.os.fsync',side_effect=OSError('fixture')):
        with self.assertRaises(OSError):store.chunk(token,0,b'x'*CHUNK)
      self.assertEqual(store.begin(encode(v))['files'][0]['offset'],0);self.assertIsNone(store.claim())
      r=store.chunk(token,0,b'x'*CHUNK);store.seal(token,encode({'size':CHUNK,'chain':r['chain']}))
      self.assertEqual(Path(store.claim()['files']['rlog.zst']).read_bytes(),b'x'*CHUNK)
