"""Temporary DB/typed event fixtures only; no live upload or AI call."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from openpilot.system.pv5_incidents.priority import *
from openpilot.system.pv5_incidents.core import ALERTS, Pending, encode, SpeedProof
from openpilot.system.pv5_incidents.runtime import Reporter
from tools.pv5_incidents.receiver import Inbox
from tools.pv5_archive.dispatch import ArchiveDispatch
from tools.pv5_archive.store import Store


class PriorityTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)

  def fact(self, code="controlsMismatch", **changes):
    return dict(vehicle="KIA_PV5", kind="system_alert", process="selfdrived", code=code,
                source_commit="a"*40, severity=3) | changes

  def test_allowlist_is_typed_known_schema_not_alert_color_or_severity_guess(self):
    self.assertLessEqual(REPORTABLE_ALERTS, ALERTS)
    for code in REPORTABLE_ALERTS:
      self.assertTrue(reportable(self.fact(code)))
    for code in ("pedalPressed", "wrongGear", "reverseGear", "parkBrake", "laneChange", "laneChangeBlocked",
                 "preLaneChangeLeft", "preLaneChangeRight", "trafficStopping", "radarCutin", "pv5SccDecelInfo",
                 "pv5SccDecelSilent", "selfdriveInitializing", "gasPressedOverride", "steerSaturated"):
      self.assertFalse(reportable(self.fact(code)))  # Even forged severity 3 cannot promote ordinary codes.
    self.assertFalse(reportable(self.fact(code=[])))

  def test_expected_shutdowns_not_crashes_but_fault_exits_kept(self):
    for code in (0,-2,-15):self.assertFalse(reportable(self.fact(code,kind="process_exit")))
    for code in (-11,-9,1,2,137):self.assertTrue(reportable(self.fact(code,kind="process_exit")))

  def test_legacy_ordinary_upload_is_acknowledged_without_pending_or_notification(self):
    inbox=Inbox(self.root/"inbox.db");q=Pending();q.add(self.fact("pedalPressed"),1000)
    data=encode(q.batch(1000));self.assertEqual(inbox.accept(data,1000)["accepted"],hashlib.sha256(data).hexdigest())
    self.assertEqual(inbox.pending(1000,True),[]);self.assertEqual(inbox.notifications(),[])

  def test_old_stored_ordinary_does_not_block_fresh_fault(self):
    inbox=Inbox(self.root/"inbox.db")
    with inbox.open() as db:
      for i in range(64):db.execute("INSERT INTO incidents(digest,fact,total,first,last) VALUES(?,?,1,1,1)",
                                  (str(i),json.dumps(self.fact("wrongGear"))))
    q=Pending();q.add(self.fact(),1000);inbox.accept(encode(q.batch(1000)),1000)
    self.assertEqual([r["fact"]["code"] for r in inbox.pending(1000)], ["controlsMismatch"])

  def test_reported_count_only_repeat_never_reopens_but_new_source_does(self):
    inbox=Inbox(self.root/"inbox.db");q=Pending();q.add(self.fact(),1000)
    inbox.accept(encode(q.batch(1000)),1000);row=inbox.pending(1000)[0]
    inbox.complete(row["digest"],1,"fixture", "123456789012345678",1000)
    q.add(self.fact(),1001);inbox.accept(encode(q.batch(1001)),1001)
    self.assertEqual(inbox.pending(100000),[])
    q.add(self.fact(source_commit="b"*40),1002);inbox.accept(encode(q.batch(1002)),1002)
    self.assertEqual(len(inbox.pending(100000)),1)

  def test_sender_ordinary_is_filtered_before_queue_and_network(self):
    r=Reporter(None,lambda:True,NS(hostname="example.ts.net",port=443),b"x"*32,spool=self.root/"spool")
    r.proof=SpeedProof(100,True,True,True,False,0.,0.,True)
    for _ in range(1000):r.add(self.fact("pv5SccDecelInfo"),1000)
    self.assertEqual(r.queue.rows,{})
    r.add(self.fact(),1000);self.assertEqual(len(r.queue.rows),1)

  def test_ordinary_radar_decel_and_normal_disengage_archive_are_quiet(self):
    for kind in ("radar_value_jump","new_close_target","rapid_approach","hard_deceleration","recorded_system_disable"):
      self.assertFalse(archive_reviewable({"candidates":[{"kind":kind}],"error_codes":["pedalPressed"]}))
    self.assertTrue(archive_reviewable({"candidates":[{"kind":"recorded_stock_aeb"}]}))
    self.assertTrue(archive_reviewable({"status":"analysis_failed"}))

  def test_archive_known_version_fault_once_but_new_fault_or_source_is_reviewed(self):
    store=Store(self.root/"archive",b"x"*32,reserve=0);send=Mock(return_value={"status":"accepted"});now=[10000]
    dispatch=ArchiveDispatch(store,self.root/"events",lambda:{"enabled":True,"push_policy":"analysis_only"},send,lambda:now[0])
    def result(token,code="locationdTemporaryError",source="a"*40):
      store.analyzed(token,{"status":"review_candidates","error_codes":[code],"source_commit":source,"candidates":[{"kind":"recorded_system_disable"}],"missing_services":[]})
    result("a"*64);dispatch.step();self.assertEqual(send.call_count,1)
    result("b"*64);now[0]=20000;dispatch.step();self.assertEqual(send.call_count,1);self.assertIsNone(dispatch.next_due)
    result("c"*64,source="b"*40);dispatch.step();self.assertEqual(send.call_count,2)
    result("d"*64,code="canError");now[0]+=301;dispatch.step();self.assertEqual(send.call_count,3)

  def test_historical_dispatch_also_suppresses_fault_under_new_policy(self):
    store=Store(self.root/"archive",b"x"*32,reserve=0)
    summary={"status":"review_candidates","error_codes":["locationdTemporaryError"],"source_commit":"a"*40,"candidates":[],"missing_services":[]}
    store.analyzed("a"*64,summary)
    send=Mock();dispatch=ArchiveDispatch(store,self.root/"events",lambda:{"enabled":True,"push_policy":"analysis_only"},send,lambda:10000)
    with store.db() as db:
      db.execute("INSERT INTO dispatches VALUES(?,?,?,?)",("a"*64,"old-cause",1,"accepted"));db.execute("UPDATE results SET notified=1 WHERE segment_id=?",("a"*64,))
    store.analyzed("b"*64,summary);dispatch.step();send.assert_not_called()


if __name__=="__main__":unittest.main()
