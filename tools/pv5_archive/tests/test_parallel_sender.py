import threading,time,unittest
from openpilot.system.pv5_incidents.archive_parallel import run_parallel

class Catalog:
  def __init__(self):self.done=[]
  def next(self,commit,exclude=frozenset()):
    for i in range(8):
      name='route--'+str(i)
      if name not in exclude:return {'manifest':{'segment':name}}
  def completed(self,candidate):self.done.append(candidate['manifest']['segment'])

class ParallelSenderChecks(unittest.TestCase):
  def test_four_transfers_overlap_without_duplicate_or_premature_completion(self):
    c=Catalog();barrier=threading.Barrier(4);lock=threading.Lock();state={'active':0,'maximum':0}
    def transfer(candidate,allowed):
      self.assertTrue(allowed());self.assertNotIn(candidate['manifest']['segment'],c.done)
      with lock:state['active']+=1;state['maximum']=max(state['maximum'],state['active'])
      barrier.wait(3)
      with lock:state['active']-=1
      return 100
    self.assertEqual(run_parallel(c,transfer,'a'*40,lambda:True),{'reason':'idle','bytes':800,'segments':8})
    self.assertEqual(state['maximum'],4);self.assertEqual(len(set(c.done)),8)

  def test_failure_never_marks_failed_or_cancelled_segments_complete(self):
    c=Catalog();barrier=threading.Barrier(4)
    def transfer(candidate,allowed):
      barrier.wait(3)
      if candidate['manifest']['segment']=='route--0':raise OSError('fixture lost ACK')
      while allowed():time.sleep(.005)
      raise RuntimeError('cancelled')
    with self.assertRaises(OSError):run_parallel(c,transfer,'a'*40,lambda:True)
    self.assertFalse(c.done)

  def test_permission_loss_reaches_all_workers_and_does_not_complete(self):
    c=Catalog();gate=threading.Event();gate.set();barrier=threading.Barrier(4)
    stopped=[]
    def transfer(candidate,allowed):
      barrier.wait(3);gate.clear()
      self.assertFalse(allowed());stopped.append(1)
      return 100
    with self.assertRaises(RuntimeError):run_parallel(c,transfer,'a'*40,gate.is_set)
    self.assertEqual(len(stopped),4);self.assertFalse(c.done)

  def test_parallelism_bound(self):
    for n in (0,5,True,1.5):
      with self.assertRaises(ValueError):run_parallel(Catalog(),lambda *_:0,'a'*40,lambda:True,n)
