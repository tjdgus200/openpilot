import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import zstandard
from openpilot.system.pv5_incidents.archive_catalog import Catalog
from openpilot.system.pv5_incidents.archive_offload import RawCatalog, upload
from openpilot.system.pv5_incidents.archive_protocol import PC_RAW, PRIVACY, BATCH, encode, file_id, headers, manifest
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from tools.pv5_archive.schema import full_schema
from tools.pv5_archive.store import Store, Ingress
from tools.pv5_archive.offload import RawStore, PreparationQueue
from tools.pv5_archive.prepare import prepare


class Offload(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory(prefix='pv5-offload-')
    self.root, self.key = Path(self.tmp.name), bytes(32)
    self.clean = Store(self.root / 'clean', self.key, reserve=0)
    self.raw = RawStore(self.root / 'raw', self.key, reserve=0)
    self.queue = PreparationQueue(self.raw, self.clean, sys.executable, Path(__file__).resolve().parents[3])
    self.ingress = Ingress(self.clean, self.raw)
    self.schema_context = full_schema()
    self.schema = self.schema_context.__enter__()

  def tearDown(self):
    self.schema_context.__exit__(None, None, None)
    self.tmp.cleanup()

  def log(self, index):
    folder = self.root / 'logs' / ('test--' + str(index))
    folder.mkdir(parents=True)
    frames = []
    for topic in ('rawAudioData', 'driverCameraState', 'driverEncodeData', 'driverMonitoringState', 'carState'):
      event = self.schema.Event.new_message(valid=True, logMonoTime=1000000000)
      event.init(topic)
      if topic == 'rawAudioData':
        event.rawAudioData.data = b'PRIVATE_TEST_AUDIO'
      frames.append(event.to_bytes())
    source = folder / 'rlog.zst'
    source.write_bytes(zstandard.ZstdCompressor().compress(b''.join(frames)))
    os.utime(source, (1000, 1000))
    return source

  def inline(self, request, result):
    value = prepare(**json.loads(request.read_bytes()))
    result.write_bytes(encode(value))
    return value

  def send(self, value, files):
    receipt = self.raw.begin(encode(value))
    for entry in value['files']:
      body = files[entry['name']]
      state = next(row for row in receipt['files'] if row['file_id'] == entry['file_id'])
      if not state['complete']:
        for offset in range(state['offset'], len(body), BATCH):
          state = self.raw.batch(entry['file_id'], offset, body[offset:offset+BATCH])
        self.raw.seal(entry['file_id'], encode({'size': len(body), 'chain': state['chain']}))
    return receipt

  def candidate(self, index=1):
    source = self.log(index)
    catalog = RawCatalog(Catalog(source.parent.parent, self.root / ('catalog' + str(index)), self.key, lambda: True, clock=lambda:2000))
    return source, catalog.next('a'*40)

  def test_protocol_default_rejects_raw_and_raw_route_is_opt_in(self):
    source, candidate = self.candidate()
    value = candidate['manifest']
    with self.assertRaises(ValueError):
      manifest(self.key, value)
    manifest(self.key, value, PC_RAW)
    path, body = '/v1/archive/raw/manifest', encode(value)
    with self.assertRaises(PermissionError):
      Ingress(self.clean).ingest(path, headers(self.key, path, body, now=1000), body, 1000)
    with self.assertRaises(PermissionError):
      self.ingress.ingest(path, headers(self.key, '/v1/archive/manifest', body, now=1000), body, 1000)
    self.ingress.ingest(path, headers(self.key, path, body, now=1000), body, 1000)
    self.assertIsNone(self.clean.claim())

  def test_filter_before_analysis_delete_raw_only_after_durable_clean_resume(self):
    source, candidate = self.candidate()
    original = source.read_bytes()
    receipt = self.send(candidate['manifest'], {'rlog.zst': original})
    self.assertIsNone(self.clean.claim())
    with patch.object(self.queue, 'execute', self.inline):
      self.assertTrue(self.queue.step())
    job = self.clean.claim()
    self.assertEqual(job['manifest']['privacy'], PRIVACY)
    path = Path(job['files']['rlog.zst'])
    with decoded(path) as stream:
      topics = []
      for frame in messages(stream):
        with self.schema.Event.from_bytes(frame) as event:
          topics.append(event.which())
    self.assertEqual(topics, ['driverMonitoringState', 'carState'])
    self.assertEqual(source.read_bytes(), original)
    entry = candidate['manifest']['files'][0]
    self.assertFalse(self.raw._path(entry['file_id'], entry['name']).exists())
    # Lost seal response / receiver restart does not require deleted raw data.
    self.raw.recover()
    resumed = self.raw.begin(encode(candidate['manifest']))
    self.assertTrue(resumed['files'][0]['complete'])
    state = resumed['files'][0]
    self.assertTrue(self.raw.seal(entry['file_id'], encode({'size':entry['size'], 'chain':state['chain']}))['complete'])
    self.assertFalse(self.queue.step())

  def test_failure_preserves_raw_blocks_analysis_and_reports_only_safe_failure(self):
    source, candidate = self.candidate()
    self.send(candidate['manifest'], {'rlog.zst': source.read_bytes()})
    with patch.object(self.queue, 'execute', side_effect=ValueError('untrusted payload must not leak')):
      self.queue.step()
    self.assertIsNone(self.clean.claim())
    entry = candidate['manifest']['files'][0]
    self.assertTrue(self.raw._path(entry['file_id'], entry['name']).is_file())
    with self.clean.db() as db:
      summary = db.execute('SELECT summary FROM results').fetchone()[0]
    self.assertNotIn('untrusted', summary)
    self.assertIn('pc_privacy_preprocessing_failed', summary)
    self.assertFalse(self.queue.step())

  def test_log_first_full_manifest_reuses_filtered_file_after_raw_deleted(self):
    source, candidate = self.candidate()
    self.send(candidate['manifest'], {'rlog.zst': source.read_bytes()})
    with patch.object(self.queue, 'execute', self.inline) as call:
      self.queue.step()
    value = dict(candidate['manifest'], files=list(candidate['manifest']['files']))
    video = b'elementary-video-fixture'
    entry = {'name':'fcamera.hevc', 'size':len(video), 'mtime_ns':1000000000000}
    entry['file_id'] = file_id(self.key, value['segment'], entry['name'], entry['size'], entry['mtime_ns'], PC_RAW)
    value['files'].append(entry)
    self.send(value, {'rlog.zst':source.read_bytes(), 'fcamera.hevc':video})
    with patch.object(self.queue, 'execute', side_effect=self.inline) as execute:
      self.queue.step()
      self.assertEqual(execute.call_count, 1)
    with self.clean.db() as db:
      self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 2)
      self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 2)

  def test_actual_preprocessing_child(self):
    source, candidate = self.candidate()
    self.send(candidate['manifest'], {'rlog.zst':source.read_bytes()})
    self.queue.step()
    self.assertIsNotNone(self.clean.claim())

  @unittest.skipIf(os.name == 'nt', 'device sender uses Linux source/permission APIs')
  @unittest.skipIf(os.name == 'nt', 'device sender uses Linux source/permission APIs')
  def test_four_senders_opaque_upload_and_concurrent_pc_filter(self):
    for index in range(4):
      self.log(index)
    catalog = Catalog(self.root/'logs', self.root/'cursor', self.key, lambda:True, clock=lambda:2000)
    ingress = self.ingress
    class Wire:
      def __init__(self, *args, **kwargs):
        self.before_send = lambda:True
      def request(self, path, body):
        self.before_send()
        import time
        return ingress.ingest(path, headers(bytes(32), path, body), body, time.time())
      def close(self):
        pass
    result = upload([catalog], None, self.key, lambda:True, ['wifi'], 'a'*40,
                    transport_factory=Wire, profile=lambda:'wifi')
    self.assertEqual(result['segments'], 4)
    self.assertIsNone(self.clean.claim())
    with ThreadPoolExecutor(4) as pool:
      self.assertEqual(list(pool.map(lambda _:self.queue.step(), range(4))), [True]*4)
    with self.clean.db() as db:
      self.assertEqual(db.execute("SELECT count(*) FROM segments WHERE status='ready'").fetchone()[0], 4)
    self.assertIsNone(catalog.next('a'*40))

  def test_combined_quota_rejects_before_any_raw_write(self):
    from tools.pv5_archive.store import CapacityError
    source, candidate = self.candidate()
    self.raw.quota = 4 * 512 * 1024**2
    with self.assertRaises(CapacityError):
      self.raw.begin(encode(candidate['manifest']))
    self.assertFalse(list(self.raw.objects.glob('*/*')))

  def test_raw_digest_distinct_and_source_reopen_blocks_read(self):
    from openpilot.system.pv5_incidents.archive_catalog import SourceChanged
    source, candidate = self.candidate()
    entry = candidate['manifest']['files'][0]
    self.assertNotEqual(entry['file_id'], file_id(self.key, candidate['manifest']['segment'], entry['name'], entry['size'], entry['mtime_ns']))
    raw = RawCatalog(Catalog(source.parent.parent, self.root/'other-cursor', self.key, lambda:True, clock=lambda:2000))
    (source.parent/'rlog.lock').touch()
    with self.assertRaises(SourceChanged):
      raw.read(candidate, entry, 0, 64)

  def test_native_aac_strip_preserves_video_packets(self):
    import shutil, subprocess
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
      self.skipTest('native ffmpeg unavailable')
    source = self.root/'qcamera.ts'
    subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=1000', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-f', 'mpegts', str(source)], check=True, capture_output=True)
    before = source.read_bytes()
    output = self.root/'sanitized.ts'
    prepare(source, output, ffmpeg=ffmpeg, ffprobe=ffprobe)
    def video_hash(path):
      return subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(path),'-map','0:v:0','-c:v','copy',
                              '-f','hash','-hash','sha256','-'],capture_output=True,check=True).stdout
    kinds = json.loads(subprocess.run([ffprobe,'-v','error','-show_entries','stream=codec_type','-of','json',str(output)],
                                      capture_output=True,check=True).stdout)
    self.assertEqual([row['codec_type'] for row in kinds['streams']], ['video'])
    self.assertEqual(video_hash(source), video_hash(output))
    self.assertEqual(source.read_bytes(), before)
    # Also exercise the real Windows Job/process-tree path including ffmpeg.
    request, result, second = self.root/'media-job.json', self.root/'media-result.json', self.root/'sanitized-child.ts'
    request.write_bytes(encode({'source':str(source),'output':str(second),'reserve':0,'maximum':512*1024**2,
                               'ffmpeg':ffmpeg,'ffprobe':ffprobe}))
    self.queue.execute(request, result)
    self.assertEqual(video_hash(source), video_hash(second))

  def test_four_keepalive_upload_connections_leave_incident_slot(self):
    import http.client, threading
    from http.server import BaseHTTPRequestHandler
    from tools.pv5_archive.store import BoundedHTTPServer, handler
    class Incident(BaseHTTPRequestHandler):
      def log_message(self, *args):
        pass
      def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.send_header('Content-Length','2')
        self.end_headers()
        self.wfile.write(b'{}')
    server = BoundedHTTPServer(('127.0.0.1', 0), handler(Incident, self.ingress))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source, candidate = self.candidate()
    path, body = '/v1/archive/raw/manifest', encode(candidate['manifest'])
    connections = []
    try:
      for _ in range(4):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        connections.append(conn)
        conn.request('POST', path, body, headers(self.key,path,body))
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
      conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
      connections.append(conn)
      conn.request('POST','/v1/incident',b'{}')
      response = conn.getresponse()
      self.assertEqual(response.status,200)
      response.read()
    finally:
      for conn in connections:
        conn.close()
      server.shutdown()
      server.server_close()
      thread.join(3)

  def test_policy_off_keeps_raw_endpoint_absent(self):
    from tools.pv5_archive.offload import enable
    self.assertIsNone(enable(self.clean, {}, self.root))
    self.assertIsNone(enable(self.clean, {'pc_preprocess_temporary_raw':False}, self.root))

  def test_retired_raw_reservation_released_after_clean_seal(self):
    source, candidate = self.candidate()
    self.send(candidate['manifest'], {'rlog.zst':source.read_bytes()})
    self.assertGreater(self.raw.reserved_bytes(), 0)
    with patch.object(self.queue, 'execute', self.inline):
      self.queue.step()
    self.assertEqual(self.raw.reserved_bytes(), 0)
    self.assertGreater(self.clean.reserved_bytes(), 0)
