import hashlib,http.client,json,os,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from openpilot.system.pv5_incidents.archive_protocol import BATCH,CHUNK,ZERO_CHAIN,advance,encode,headers
from openpilot.system.pv5_incidents.archive_catalog import Catalog,SourceChanged
from openpilot.system.pv5_incidents.archive_sanitize import PrivacyCatalog,Sanitizer
from openpilot.system.pv5_incidents.archive_worker import Copier,Paused
from tools.pv5_archive.store import Store,Ingress,OffsetError,CapacityError,BoundedHTTPServer,handler
from tools.pv5_archive.tests.test_store import KEY,description

class BulkStoreChecks(unittest.TestCase):
 def test_legacy_partial_batch_lost_ack_reopen_and_legacy_tail(self):
  data=bytes(range(251))*(3*BATCH//251+2)+b'tail'
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);s=Store(root,KEY,reserve=0);v=description({'rlog.zst':data});token=v['files'][0]['file_id']
   self.assertEqual(s.begin(encode(v))['max_batch_bytes'],BATCH)
   s.chunk(token,0,data[:CHUNK]);r=s.batch(token,CHUNK,data[CHUNK:CHUNK+BATCH])
   s=Store(root,KEY,reserve=0);state=s.begin(encode(v))['files'][0]
   self.assertEqual(state['offset'],CHUNK+BATCH if len(data)>CHUNK+BATCH else len(data))
   self.assertEqual(s.batch(token,CHUNK,data[CHUNK:CHUNK+BATCH]),r)
   chain=ZERO_CHAIN
   for off in range(0,state['offset'],CHUNK):chain=advance(chain,data[off:off+CHUNK])
   self.assertEqual(chain,state['chain'])
   for off in range(state['offset'],len(data),CHUNK):r=s.chunk(token,off,data[off:off+CHUNK]);chain=advance(chain,data[off:off+CHUNK])
   s.seal(token,encode({'size':len(data),'chain':chain}))
   self.assertEqual(Path(s.claim()['files']['rlog.zst']).read_bytes(),data)
 def test_invalid_batch_and_space_are_rejected_without_advancing(self):
  with tempfile.TemporaryDirectory() as t:
   s=Store(Path(t),KEY,reserve=0);v=description({'rlog.zst':b'x'*(BATCH+CHUNK)});token=v['files'][0]['file_id'];s.begin(encode(v))
   for off,body in ((0,b'x'*(BATCH+1)),(0,b'x'*(BATCH-1)),(1,b'x'*BATCH),(False,b'x'*BATCH)):
    with self.assertRaises(ValueError):s.batch(token,off,body)
   with self.assertRaises(OffsetError):s.batch(token,CHUNK,b'x'*BATCH)
   with patch('tools.pv5_archive.store.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
    with self.assertRaises(CapacityError):s.batch(token,0,b'x'*BATCH)
   self.assertEqual(s.begin(encode(v))['files'][0]['offset'],0)
 def test_interrupted_fsync_tail_and_conflicting_duplicate(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);s=Store(root,KEY,reserve=0);v=description({'rlog.zst':b'x'*(BATCH+1)});token=v['files'][0]['file_id'];s.begin(encode(v))
   with patch('tools.pv5_archive.store.os.fsync',side_effect=OSError('test interrupted checkpoint')):
    with self.assertRaises(OSError):s.batch(token,0,b'x'*BATCH)
   s=Store(root,KEY,reserve=0);self.assertEqual(s.begin(encode(v))['files'][0]['offset'],0)
   s.batch(token,0,b'x'*BATCH)
   with self.assertRaises(ValueError):s.batch(token,0,b'y'*BATCH)
   r=s.chunk(token,BATCH,b'x');s.seal(token,encode({'size':BATCH+1,'chain':r['chain']}))
   self.assertEqual(Path(s.claim()['files']['rlog.zst']).stat().st_size,BATCH+1)
 def test_batch_authentication_replay_body_and_offset(self):
  with tempfile.TemporaryDirectory() as t:
   s=Store(Path(t),KEY,reserve=0);v=description({'rlog.zst':b'x'*BATCH});token=v['files'][0]['file_id'];s.begin(encode(v));i=Ingress(s)
   path='/v1/archive/batch/'+token+'/0';b=b'x'*BATCH;h=headers(KEY,path,b,now=1700000000)
   for changed,body in ((path,b'y'*BATCH),(path[:-1]+'1',b)):
    with self.assertRaises(PermissionError):i.ingest(changed,h,body,1700000000)
   i.ingest(path,h,b,1700000000)
   with self.assertRaises(PermissionError):i.ingest(path,h,b,1700000000)
 def test_actual_http_batch_and_legacy_limits(self):
  from tools.pv5_incidents.receiver import Inbox,Ingress as OldIngress,handler as old_handler
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);s=Store(root/'pc',KEY,reserve=0);v=description({'rlog.zst':b'x'*BATCH});token=v['files'][0]['file_id'];s.begin(encode(v))
   server=BoundedHTTPServer(('127.0.0.1',0),handler(old_handler(OldIngress(KEY,Inbox(root/'old.sqlite'))),Ingress(s)));threading.Thread(target=server.serve_forever,daemon=True).start()
   try:
    c=http.client.HTTPConnection(*server.server_address,timeout=3);path='/v1/archive/batch/'+token+'/0';b=b'x'*BATCH
    c.request('POST',path,b,headers=headers(KEY,path,b));r=c.getresponse();self.assertEqual(r.status,200);self.assertEqual(json.loads(r.read())['offset'],BATCH);c.close()
    for path,size in (('/v1/archive/chunk/'+token+'/0',CHUNK+1),('/v1/archive/batch/'+token+'/0',BATCH+1),('/v1/incident',8193)):
     c=http.client.HTTPConnection(*server.server_address,timeout=3);c.putrequest('POST',path);c.putheader('Content-Length',str(size));c.endheaders();r=c.getresponse();self.assertEqual(r.status,413);r.read();c.close()
   finally:server.shutdown();server.server_close()

class BulkCopierChecks(unittest.TestCase):
 def scenario(self,home,capability=True,lose_ack=False,change_source=False,lose_permission=False):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);folder=root/'logs'/'00000341--1810673eb4--14';folder.mkdir(parents=True);source=folder/'fcamera.hevc';data=b'v'*(BATCH+CHUNK+17);source.write_bytes(data);os.utime(source,(1000,1000));allowed=[True]
   raw=Catalog(folder.parent,root/'cursor.sqlite',KEY,lambda:allowed[0],clock=lambda:2000);safe=PrivacyCatalog(raw,Sanitizer(root/'cache',raw.check));s=Store(root/'pc',KEY,reserve=0);paths=[];ack_lost=[False]
   class Local:
    before_send=None
    def request(inner,path,body):
     inner.before_send();paths.append(path)
     if path.endswith('manifest'):
      result=s.begin(body)
      if not capability:result.pop('max_batch_bytes')
      return result
     if '/batch/' in path or '/chunk/' in path:
      token,offset=path.rsplit('/',2)[1:];result=(s.batch if '/batch/' in path else s.chunk)(token,int(offset),body)
      if lose_ack and not ack_lost[0]:ack_lost[0]=True;raise Paused('network')
      return result
     return s.seal(path.rsplit('/',1)[1],body)
    def close(inner):pass
   tr=Local();reads=[0];original=safe.read
   def read(*args):
    out=original(*args);reads[0]+=1
    if reads[0]==2:
     if lose_permission:allowed[0]=False
     if change_source:source.unlink()
    return out
   safe.read=read
   def run():return Copier(safe,tr,lambda:allowed[0],['trusted'],profile=lambda:'trusted',rate=10**9,cpu=lambda:0,home_unlimited=home).run('a'*40)
   if change_source or lose_permission:
    with self.assertRaises((Paused,SourceChanged,FileNotFoundError)):run()
    self.assertFalse(any('/batch/' in path for path in paths));self.assertIsNone(s.claim());return
   if lose_ack:
    with self.assertRaises(Paused):run()
   run();self.assertEqual(Path(s.claim()['files']['fcamera.hevc']).read_bytes(),data);self.assertEqual(source.read_bytes(),data)
   uploads=[x for x in paths if '/batch/' in x or '/chunk/' in x]
   self.assertEqual(any('/batch/' in x for x in uploads),home and capability)
   if not lose_ack:self.assertEqual(len(uploads),2 if home and capability else 18)
 def test_home_negotiation(self):self.scenario(True)
 def test_old_server_fallback(self):self.scenario(True,False)
 def test_vehicle_keeps_legacy_even_on_new_server(self):self.scenario(False)
 def test_lost_batch_ack_resume(self):self.scenario(True,lose_ack=True)
 def test_source_deleted_before_batch_is_not_sent(self):self.scenario(True,change_source=True)
 def test_permission_lost_before_batch_is_not_sent(self):self.scenario(True,lose_permission=True)
if __name__=='__main__':unittest.main()
