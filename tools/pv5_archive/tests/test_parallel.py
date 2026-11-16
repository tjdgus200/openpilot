import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.pv5_archive.service import AnalysisQueue
from tools.pv5_archive.store import Store
from tools.pv5_archive.dispatch import ArchiveDispatch
from tools.pv5_archive.tests.test_store import KEY, description
from openpilot.system.pv5_incidents.archive_protocol import encode, advance, ZERO_CHAIN


class ParallelChecks(unittest.TestCase):
  def seal(self, store, value):
    token=store.begin(encode(value))['segment_id']
    for entry in value['files']:
      data=b'video' if entry['name']=='fcamera.hevc' else b'log'
      store.chunk(entry['file_id'],0,data)
      store.seal(entry['file_id'],encode({'size':len(data),'chain':advance(ZERO_CHAIN,data)}))
    return token

  def test_four_backlog_jobs_overlap_and_each_claim_only_once(self):
    with TemporaryDirectory() as tmp:
      store=Store(Path(tmp)/'store',KEY,reserve=0)
      for i in range(8):
        value=description({'fcamera.hevc':b'video'})
        value['segment']=value['segment'].rsplit('--',1)[0]+'--'+str(i)
        from openpilot.system.pv5_incidents.archive_protocol import file_id
        for entry in value['files']:
          entry['file_id']=file_id(KEY,value['segment'],entry['name'],entry['size'],entry['mtime_ns'])
        self.seal(store,value)
      q=AnalysisQueue(store,sys.executable,Path(__file__).resolve().parents[3],workers=4)
      state={'active':0,'maximum':0,'calls':0};guard=threading.Lock()
      def child(args,**kw):
        with guard:
          state['active']+=1;state['maximum']=max(state['maximum'],state['active']);state['calls']+=1
        time.sleep(.08)
        output=Path(args[args.index('--output')+1]);output.write_text(json.dumps({'status':'no_threshold_candidate','candidates':[]}))
        with guard:state['active']-=1
        return subprocess.CompletedProcess(args,0)
      def done():
        with store.db() as db:n=db.execute('SELECT count(*) FROM results').fetchone()[0]
        if n==8:q.stopping=True;q.wake()
      q.notify=done
      with patch('tools.pv5_archive.service.subprocess.run',side_effect=child):
        thread=threading.Thread(target=q.run);thread.start();thread.join(5)
        q.stopping=True;q.wake();thread.join(2)
      self.assertFalse(thread.is_alive());self.assertEqual(state['calls'],8);self.assertEqual(state['maximum'],4)
      with store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM results').fetchone()[0],8)

  def test_sealed_log_is_claimed_before_video_then_full_job_reuses_cache(self):
    import zstandard
    from tools.pv5_archive.schema import full_schema
    with TemporaryDirectory() as tmp, full_schema() as schema:
      events=[]
      for topic in ('initData','carParams','modelV2','radarState','carOutput','onroadEvents','carState'):
        event=schema.Event.new_message(valid=True,logMonoTime=100000000000)
        value=event.init(topic,0) if topic=='onroadEvents' else event.init(topic)
        if topic=='initData':value.gitCommit='b'*40
        if topic=='carParams':value.carFingerprint='KIA_PV5'
        if topic=='carState':value.canValid=True;value.vEgo=10;value.aEgo=-4
        events.append(event.to_bytes())
      data=zstandard.ZstdCompressor().compress(b''.join(events))
      store=Store(Path(tmp)/'s',KEY,reserve=0)
      value=description({'rlog.zst':data,'fcamera.hevc':b'video'})
      token=store.begin(encode(value))['segment_id'];log=next(e for e in value['files'] if e['name']=='rlog.zst')
      store.chunk(log['file_id'],0,data);store.seal(log['file_id'],encode({'size':len(data),'chain':advance(ZERO_CHAIN,data)}))
      q=AnalysisQueue(store,sys.executable,Path(__file__).resolve().parents[3]);self.assertTrue(q.step());self.assertFalse(q.step())
      with store.db() as db:
        self.assertEqual(db.execute('SELECT status FROM segments').fetchone()[0],'uploading')
        first=json.loads(db.execute('SELECT summary FROM results').fetchone()[0]);self.assertTrue(first['awaiting_media'])
      video=next(e for e in value['files'] if e['name']=='fcamera.hevc')
      store.chunk(video['file_id'],0,b'video');store.seal(video['file_id'],encode({'size':5,'chain':advance(ZERO_CHAIN,b'video')}))
      self.assertTrue(q.step());self.assertFalse(q.step())
      with store.db() as db:
        second=json.loads(db.execute('SELECT summary FROM results').fetchone()[0]);self.assertTrue(second['road_video_present']);self.assertTrue(second['data_screen_cache_reused'])
        self.assertFalse(second.get('awaiting_media',False));self.assertEqual(db.execute('SELECT status FROM segments').fetchone()[0],'analyzed')
      self.assertEqual(first['candidates'],second['candidates'])

  def test_parallelism_budget(self):
    with TemporaryDirectory() as tmp:
      store=Store(Path(tmp)/'s',KEY,reserve=0)
      for invalid in (0,9,True,'4'):
        with self.assertRaises(ValueError):AnalysisQueue(store,sys.executable,Path(__file__).resolve().parents[3],workers=invalid)

  def test_log_screen_result_waits_for_media_without_AI(self):
    with TemporaryDirectory() as tmp:
      store=Store(Path(tmp)/'store',KEY,reserve=0);calls=[]
      store.analyzed('a'*64,{'status':'review_candidates','candidates':[{'kind':'hard_deceleration'}],'awaiting_media':True})
      dispatch=ArchiveDispatch(store,Path(tmp)/'events',lambda:{'enabled':True,'push_policy':'analysis_only'},lambda *args:calls.append(args),clock=lambda:10000)
      dispatch.step();self.assertEqual(calls,[])
      with store.db() as db:self.assertEqual(db.execute('SELECT notified FROM results').fetchone()[0],-2)

if __name__=='__main__':unittest.main()
