import json
from pathlib import Path
import sys

import pytest
import zstandard

from openpilot.system.pv5_incidents.archive_protocol import PRIVACY, ZERO_CHAIN, advance, encode, file_id
from tools.pv5_archive.analyze import analyze
from tools.pv5_archive.dispatch import ArchiveDispatch, causes, encoded_fault
from tools.pv5_archive.review import selected_samples
from tools.pv5_archive.schema import full_schema
from tools.pv5_archive.service import AnalysisQueue
from tools.pv5_archive.store import Store
from tools.pv5_archive.tests.test_store import KEY

ANCHOR = 20_000_000_000
SOURCE = 'f' * 40
CLIP = 'event-' + 'b' * 64 + '--0'
BULK = '2026-11-19--00-00-00--0'
CODE = Path(__file__).resolve().parents[3]


def frames(schema, *, issue='locationdTemporaryError', exit_code=None, outside=False, sensitive=None):
  init = schema.Event.new_message(valid=True, logMonoTime=1)
  init.init('initData').gitCommit = SOURCE
  cp = schema.Event.new_message(valid=True, logMonoTime=1)
  cp.init('carParams').carFingerprint = 'KIA_PV5'
  camera = schema.Event.new_message(valid=False, logMonoTime=ANCHOR - 2_000_000_000)
  camera.init('cameraOdometry')
  pose = schema.Event.new_message(valid=True, logMonoTime=ANCHOR - 1_000_000_000)
  pose.init('livePose').inputsOK = False
  if exit_code is None:
    event = schema.Event.new_message(valid=True, logMonoTime=ANCHOR)
    alert = event.init('onroadEvents', 1)[0]
    alert.name, alert.noEntry = issue, True
  else:
    event = schema.Event.new_message(valid=True, logMonoTime=ANCHOR)
    process = event.init('managerState').init('processes', 1)[0]
    process.name, process.pid = 'modeld', 12
    process.shouldBeRunning, process.running, process.exitCode = True, False, exit_code
  car = schema.Event.new_message(valid=True, logMonoTime=ANCHOR + (6 if outside else 3) * 1_000_000_000)
  car.init('carState').canValid = True
  records = [init, cp, camera, pose, event, car]
  if sensitive == 'initData':
    init.initData.dongleId = 'fixture-id'
  if sensitive == 'roadCameraState':
    raw = schema.Event.new_message(valid=True, logMonoTime=ANCHOR)
    raw.init('roadCameraState').image = b'fixture-image'
    records.append(raw)
  if sensitive == 'deviceState':
    raw = schema.Event.new_message(valid=True, logMonoTime=ANCHOR)
    raw.init('deviceState').networkInfo.extra = 'fixture-network'
    records.append(raw)
  return zstandard.ZstdCompressor().compress(b''.join(frame.to_bytes() for frame in records))


def begin(store, segment, data):
  item = {'name': 'rlog.zst', 'size': len(data), 'mtime_ns': ANCHOR,
          'file_id': file_id(KEY, segment, 'rlog.zst', len(data), ANCHOR)}
  value = {'version': 1, 'privacy': PRIVACY, 'segment': segment, 'files': [item], 'uploader_commit': 'a' * 40}
  token = store.begin(encode(value))['segment_id']
  store.chunk(item['file_id'], 0, data)
  store.seal(item['file_id'], encode({'size': len(data), 'chain': advance(ZERO_CHAIN, data)}))
  return token


def test_sealed_clip_is_analyzed_first_and_dispatched_without_waiting_for_video(tmp_path):
  store = Store(tmp_path / 'private', KEY, reserve=0)
  with full_schema() as schema:
    begin(store, BULK, frames(schema, issue='wrongGear'))
    token = begin(store, CLIP, frames(schema))

  queue = AnalysisQueue(store, sys.executable, CODE)
  assert queue.step()
  result = json.loads((queue.folder / (token + '.json')).read_bytes())
  assert result['source_commit'] == SOURCE
  assert result['status'] == 'review_candidates'
  assert result['awaiting_media'] is False and result['road_video_present'] is False
  selected = selected_samples(result, 0)
  assert selected['samples'][0]['t'] == 5
  evidence = selected['event_diagnostics']
  assert evidence['anchor_recognized']
  assert evidence['anchor_seconds'] == 2
  assert evidence['observed_seconds'] == 5
  assert evidence['services']['cameraOdometry']['first_invalid_seconds'] == 0
  assert evidence['first_observed_flags']['livePose.inputsOK'] == {'false': 1}
  assert evidence['facts'] == [{'kind': 'recorded_system_fault', 'code': 'locationdTemporaryError', 'process': None, 't': 2, 'at_anchor': True}]

  dispatched = []
  worker = ArchiveDispatch(store, tmp_path / 'events', lambda: {'enabled': True, 'push_policy': 'analysis_only'},
                           lambda path, _: dispatched.append(json.loads(path.read_bytes())) or {'status': 'accepted'},
                           clock=lambda: 10000)
  worker.step()
  worker.step()
  assert [item['segment_id'] for payload in dispatched for item in payload['items']] == [token]
  assert queue.step()  # The existing bulk upload still gets its normal turn.


@pytest.mark.parametrize(('issue', 'exit_code', 'accepted'), [
  ('wrongGear', None, False), ('pv5SccDecelSilent', None, False), (None, -15, False), (None, -9, True),
])
def test_only_typed_incidents_can_admit_a_clip(tmp_path, issue, exit_code, accepted):
  log = tmp_path / 'rlog.zst'
  with full_schema() as schema:
    log.write_bytes(frames(schema, issue=issue, exit_code=exit_code))
    result = analyze(log, schema, event_anchor=ANCHOR)
  assert result['event_diagnostics']['anchor_recognized'] is accepted
  assert bool(result['review_windows']) is accepted
  if accepted:
    assert result['event_diagnostics']['facts'] == [
      {'kind': 'recorded_process_exit', 'code': -9, 'process': 'modeld', 't': 2, 'at_anchor': True},
    ]


def test_clip_with_out_of_window_records_cannot_enter_event_review(tmp_path):
  log = tmp_path / 'rlog.zst'
  with full_schema() as schema:
    log.write_bytes(frames(schema, outside=True))
    with pytest.raises(ValueError, match='invalid_event_clip_time_range'):
      analyze(log, schema, event_anchor=ANCHOR)

  store = Store(tmp_path / 'private', KEY, reserve=0)
  token = begin(store, CLIP, log.read_bytes())
  queue = AnalysisQueue(store, sys.executable, CODE)
  assert queue.step()
  result = json.loads((queue.folder / (token + '.json')).read_bytes())
  assert result['status'] == 'analysis_failed' and result['event_clip']
  sent = []
  worker = ArchiveDispatch(store, tmp_path / 'events', lambda: {'enabled': True, 'push_policy': 'analysis_only'},
                           lambda path, _: sent.append(path), clock=lambda: 10000)
  worker.step()
  assert sent == []


def test_distinct_process_exits_do_not_share_cooldown_or_suppress_urgent_aeb():
  def result(code):
    return {
      'event_clip': True, 'status': 'review_candidates', 'source_commit': SOURCE,
      'candidates': [{'kind': 'recorded_process_exit'}],
      'event_diagnostics': {'facts': [{'kind': 'recorded_process_exit', 'process': 'modeld', 'code': code}]},
    }

  first, second = result(-9), result(-11)
  assert set(causes(first)).isdisjoint(causes(second))
  assert encoded_fault(first) != encoded_fault(second)
  second['candidates'].append({'kind': 'recorded_stock_aeb'})
  second['event_diagnostics']['facts'].append({'kind': 'recorded_stock_aeb', 'code': 'stockAeb'})
  assert encoded_fault(second) is None



def test_many_quiet_results_cannot_hide_the_next_critical_clip(tmp_path):
  store = Store(tmp_path / 'private', KEY, reserve=0)
  for index in range(64):
    store.analyzed(f'{index:064x}', {'event_clip': True, 'status': 'insufficient_data', 'candidates': []})
  urgent = 'f' * 64
  store.analyzed(urgent, {
    'event_clip': True, 'status': 'review_candidates', 'source_commit': SOURCE,
    'candidates': [{'kind': 'recorded_critical_alert'}], 'error_codes': ['fcw'],
  })
  sent = []
  dispatch = ArchiveDispatch(store, tmp_path / 'events', lambda: {'enabled': True, 'push_policy': 'analysis_only'},
                             lambda path, _: sent.append(json.loads(path.read_bytes())) or {'status': 'accepted'},
                             clock=lambda: 10000)
  dispatch.step()
  assert [item['segment_id'] for payload in sent for item in payload['items']] == [urgent]


def test_native_sender_archive_protocol_and_pc_analysis_end_to_end(tmp_path):
  pytest.importorskip('resource')
  from openpilot.system.pv5_incidents.archive_protocol import headers
  from openpilot.system.pv5_incidents.archive_worker import Copier
  from openpilot.system.pv5_incidents.stationary_event_clip import ClipCatalog
  from tools.pv5_archive.store import Ingress
  import os

  source = tmp_path / 'logs' / BULK
  source.mkdir(parents=True)
  store = Store(tmp_path / 'private', KEY, reserve=0)
  ingress = Ingress(store)

  class TestTransport:
    def request(self, endpoint, data):
      self.before_send()
      return ingress.ingest(endpoint, headers(KEY, endpoint, data, now=1000), data, 1000)

    def close(self):
      pass

  with full_schema() as schema:
    log = source / 'rlog.zst'
    log.write_bytes(frames(schema))
    os.utime(log, (900, 900))
    original = log.read_bytes()
    catalog = ClipCatalog(source.parent, tmp_path / 'vehicle.sqlite', KEY, lambda: True, lambda: True,
                          schema=schema, clock=lambda: 1000)
    wire = Copier(catalog, TestTransport(), lambda: True, ['known'], profile=lambda: 'known', rate=10**9, cpu=lambda: 0)
    assert wire.run('a' * 40)['segments'] == 1
    assert log.read_bytes() == original

  queue = AnalysisQueue(store, sys.executable, CODE)
  assert queue.step() and not queue.step()
  with store.db() as db:
    result = json.loads(db.execute('SELECT summary FROM results').fetchone()['summary'])
  assert result['status'] == 'review_candidates'
  assert result['event_diagnostics']['services']['cameraOdometry']['invalid'] == 1
  assert result['event_diagnostics']['anchor_recognized']


def test_diagnostic_mode_preempts_only_bulk_worker_and_allows_fallback():
  from openpilot.system.pv5_incidents.runtime import Reporter

  class Worker:
    def __init__(self, active=False):
      self.child = object() if active else None
      self.priority_clips = False
      self.starts = False
      self.ticks = 0
      self.cancels = 0

    def tick(self, sm, now, *, before_start=None):
      self.ticks += 1
      if self.starts and self.child is None:
        if before_start is not None:
          before_start()
        self.child = object()

    def cancel(self):
      self.cancels += 1
      self.child = None

  observer = object.__new__(Reporter)
  observer.sm = object()
  observer.archive = Worker(active=True)
  observer.events = Worker()
  observer.events.priority_clips = True
  observer.tick_uploads(0)
  assert observer.archive.cancels == 0 and observer.archive.ticks == 1
  observer.events.starts = True
  observer.tick_uploads(1)
  assert observer.archive.cancels == 1 and observer.archive.ticks == 1
  assert observer.events.child is not None
  observer.events.child = None
  observer.events.starts = False
  observer.tick_uploads(2)
  assert observer.archive.ticks == 2


@pytest.mark.parametrize('topic', ['initData', 'roadCameraState', 'deviceState'])
def test_receiver_does_not_analyze_unsanitized_clip_fields(tmp_path, topic):
  path = tmp_path / 'rlog.zst'
  with full_schema() as schema:
    path.write_bytes(frames(schema, sensitive=topic))
    with pytest.raises(ValueError, match='private_event_clip_'):
      analyze(path, schema, event_anchor=ANCHOR)
