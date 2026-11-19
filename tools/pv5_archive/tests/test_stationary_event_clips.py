"""Native rlog clipping, permissions, privacy, and retry tests for the vehicle sender."""

import os
from pathlib import Path
import re
import tempfile
import unittest

from openpilot.system.pv5_incidents.archive_protocol import PRIVACY, manifest
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from openpilot.system.pv5_incidents.archive_catalog import SourceChanged
from openpilot.system.pv5_incidents.archive_sanitize import identity
from openpilot.system.pv5_incidents.stationary_event_clip import ClipCatalog, Source
from tools.pv5_archive.schema import full_schema


NS = 10**9


class ClipTests(unittest.TestCase):
  def setUp(self):
    context = full_schema()
    self.schema = context.__enter__()
    self.addCleanup(context.__exit__, None, None, None)
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    self.root = Path(directory.name) / 'logs'
    self.root.mkdir()
    self.database = self.root.parent / 'archive.sqlite'
    self.allowed = True
    self.key = bytes(32)

  def permit(self):
    if not self.allowed:
      raise SourceChanged('state')
    return True

  def catalog(self):
    return ClipCatalog(self.root, self.database, self.key, lambda: self.allowed, self.permit, self.schema, clock=lambda: 1000)

  def message(self, topic, seconds, length=None):
    event = self.schema.Event.new_message(logMonoTime=round(seconds * NS), valid=True)
    event.init(topic, length) if length is not None else event.init(topic)
    return event

  def car(self, seconds, *, accel=0, aeb=False):
    event = self.message('carState', seconds)
    event.carState.canValid = True
    event.carState.vEgo = 12.
    event.carState.aEgo = accel
    event.carState.stockAeb = aeb
    return event

  def segment(self, number, *events):
    path = self.root / f'drive--{number}'
    path.mkdir()
    source = path / 'rlog'
    source.write_bytes(b''.join(event.to_bytes() for event in events))
    os.utime(source, (900, 900))
    return source

  def read(self, candidate):
    result = {}
    with decoded(candidate['output']) as stream:
      for frame in messages(stream):
        for secret in (b'private-dongle', b'private-vin', b'private text', b'private-network', b'private-image'):
          self.assertNotIn(secret, frame)
        with self.schema.Event.from_bytes(frame) as event:
          topic = event.which()
          if topic == 'initData':
            self.assertEqual(str(event.initData.gitCommit), 'a' * 40)
            self.assertEqual(str(event.initData.dongleId), '')
          if topic == 'carParams':
            self.assertEqual(str(event.carParams.carFingerprint), 'KIA_PV5')
            self.assertEqual(str(event.carParams.carVin), '')
          result.setdefault(topic, []).append(event.logMonoTime / NS)
    return result

  def test_urgent_and_crash_samples_survive_segment_window_limit(self):
    events = []
    for seconds in range(1, 10):
      fault = self.message('onroadEvents', seconds, 1)
      fault.onroadEvents[0].name = 'locationdTemporaryError'
      fault.onroadEvents[0].softDisable = True
      events.extend((fault, self.message('onroadEvents', seconds + 0.25, 0)))
    crash = self.message('managerState', 10)
    crash.managerState.init('processes', 1)[0].from_dict(
      {'name': 'modeld', 'pid': 12, 'shouldBeRunning': True, 'exitCode': -11})
    path = self.segment(0, *events, crash, self.car(11, aeb=True))
    centers, _, _ = self.catalog().scan(Source(path, identity(path)))
    self.assertEqual(centers, [seconds * NS for seconds in (1, 2, 3, 4, 5, 6, 10, 11)])

  def test_exact_cross_boundary_clip_and_restart(self):
    init = self.message('initData', 1)
    init.initData.gitCommit = 'a' * 40
    init.initData.dongleId = 'private-dongle'
    car_params = self.message('carParams', 4)
    car_params.carParams.carFingerprint = 'KIA_PV5'
    car_params.carParams.carVin = 'private-vin'
    old = self.segment(0, init, car_params, self.car(9), self.car(10), self.car(12), self.car(14))
    alarm = self.message('onroadEvents', 15, 1)
    alarm.onroadEvents[0].name = 'locationdTemporaryError'
    alarm.onroadEvents[0].softDisable = True
    camera = self.message('roadCameraState', 16)
    camera.roadCameraState.frameId = 42
    camera.roadCameraState.image = b'private-image'
    thermal = self.message('deviceState', 17)
    thermal.deviceState.thermalStatus = 'red'
    thermal.deviceState.networkInfo.extra = 'private-network'
    parked = self.message('parkedDashcamState', 18)
    parked.parkedDashcamState.inferencePaused = True
    private_text = self.schema.Event.new_message(logMonoTime=15_400_000_000, valid=True, logMessage='private text')
    new = self.segment(1, self.car(14.5), alarm, self.message('rawAudioData', 15.1),
                       self.message('driverCameraState', 15.2), self.message('roadEncodeData', 15.3),
                       private_text, camera, thermal, parked, self.car(18), self.car(20), self.car(21))
    originals = [source.read_bytes() for source in (old, new)]
    catalog = self.catalog()
    candidate = catalog.next('f' * 40)
    wire = candidate['manifest']
    manifest(self.key, wire)
    self.assertRegex(wire['segment'], re.compile(r'^event-[0-9a-f]{64}--0$'))
    self.assertEqual(wire['privacy'], PRIVACY)
    self.assertEqual([(item['name'], item['mtime_ns']) for item in wire['files']], [('rlog.zst', 15 * NS)])
    self.assertEqual(self.read(candidate), {
      'carState': [10.0, 12.0, 14.0, 14.5, 18.0, 20.0], 'onroadEvents': [15.0], 'initData': [1.0], 'carParams': [4.0],
      'roadCameraState': [16.0], 'deviceState': [17.0], 'parkedDashcamState': [18.0],
    })
    entry = wire['files'][0]
    chunk = catalog.read(candidate, entry, 0, 64)
    self.allowed = False
    with self.assertRaises(SourceChanged):
      catalog.read(candidate, entry, 0, 64)
    self.allowed = True
    restarted = self.catalog()
    resumed = restarted.next('f' * 40)
    self.assertEqual(resumed['manifest'], wire)
    self.assertEqual(restarted.read(resumed, entry, 0, 64), chunk)
    restarted.completed(resumed)
    self.assertIsNone(restarted.next('f' * 40))
    self.assertEqual([source.read_bytes() for source in (old, new)], originals)

  def test_reportable_process_exit_only_and_missing_neighbor(self):
    def state(seconds, code):
      event = self.message('managerState', seconds)
      process = event.managerState.init('processes', 1)[0]
      process.name = 'modeld'
      process.pid = 100
      process.running = False
      process.shouldBeRunning = True
      process.exitCode = code
      return event

    untyped = self.message('onroadEvents', 6.5, 1)
    untyped.onroadEvents[0].name = 'fcw'
    self.segment(0, self.car(5, accel=-4), state(6, -15), untyped, self.car(7, accel=-4), state(9, -6), state(10, -6))
    catalog = self.catalog()
    candidate = catalog.next('f' * 40)
    self.assertEqual(candidate['manifest']['files'][0]['mtime_ns'], 9 * NS)
    self.assertEqual(self.read(candidate), {'carState': [5.0, 7.0], 'managerState': [6.0, 9.0, 10.0], 'onroadEvents': [6.5]})
    catalog.completed(candidate)
    self.assertIsNone(catalog.next('f' * 40))

  def test_locked_adjacent_waits_for_close_without_blocking_ready_event(self):
    self.segment(0, self.car(20), self.car(25, aeb=True), self.car(31))
    self.segment(1, self.car(72), self.car(78, aeb=True))
    following = self.segment(2, self.car(79), self.car(81), self.car(83))
    lock = following.with_suffix('.lock')
    lock.touch()
    catalog = self.catalog()
    ready = catalog.next('f' * 40)
    self.assertEqual(ready['center'], 25 * NS)
    catalog.completed(ready)
    self.assertIsNone(catalog.next('f' * 40))
    lock.unlink()
    boundary = self.catalog().next('f' * 40)
    self.assertEqual(boundary['center'], 78 * NS)
    self.assertEqual(self.read(boundary), {'carState': [78.0, 79.0, 81.0, 83.0]})


if __name__ == '__main__':
  unittest.main()
