import ast
import pytest
from collections import deque
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hud_quality_compare import Comparison, MAX_SAMPLES
from openpilot.common.hud_quality_status import quality_status

ROOT = Path(__file__).resolve().parents[2]


class TestCompare:
  def setup_method(self):
    self.directory = tempfile.TemporaryDirectory()
    self.path = Path(self.directory.name) / 'summary.json'
    self.now = [0.0]

  def teardown_method(self):
    self.directory.cleanup()

  def collector(self, profile=0, effective=None, error=''):
    return Comparison(profile, profile if effective is None else effective, error, path=self.path, clock=lambda: self.now[0])

  def update(self, collector, rows, session='test-session'):
    return collector.update({'sample_session': session, 'samples': rows})

  def ready(self, collector, start, gpu=25000, count=1220):
    self.now[0] = start
    self.update(collector, [])
    result = None
    for chunk in range(0, count, 20):
      rows = [(start + 15 + i * 0.05, gpu + 2000, gpu) for i in range(chunk, min(count, chunk + 20))]
      self.now[0] = rows[-1][0]
      result = self.update(collector, rows)
    return result

  def test_raw_samples_not_overlapping_p99s_and_duplicate_frames_are_ignored(self):
    collector = self.collector()
    self.update(collector, [])
    self.now[0] = 16
    rows = [(15.0, 31000, 1000), (15.05, 31000, 2000), (15.1, 31000, 3000)]
    report = self.update(collector, rows)
    assert report['rows'][0]['p99_ms'] == pytest.approx(2.98)
    assert self.update(collector, rows)['rows'][0]['n'] == 3

  def test_warmup_excludes_old_frames(self):
    collector = self.collector()
    self.update(collector, [])
    self.now[0] = 16
    report = self.update(collector, [(14.0, 99000, 99000), (15.0, 26000, 25000)])
    assert report['rows'][0]['n'] == 1
    assert report['rows'][0]['max_ms'] == 25.0
    assert report['best'] is None

  def test_ranking_needs_two_qualified_candidates_and_retains_only_same_session(self):
    first = self.collector()
    report = self.ready(first, 0.0, gpu=30000)
    assert report['state'] == 'ready'
    assert report['best'] is None
    second = self.collector(5)
    report = self.ready(second, self.now[0] + 1, gpu=25000)
    assert report['best'] == 5
    assert {row['id'] for row in report['rows']} == {0, 5}
    assert quality_status(report) is not None
    self.now[0] += 1
    changed = self.update(second, [], session='different-model/runtime')
    assert changed['rows'] == []
    assert changed['best'] is None

  def test_fallback_does_not_credit_jpeg(self):
    collector = self.collector(5, 0, 'JPEG unavailable')
    report = self.ready(collector, 0.0)
    assert report['state'] == 'fallback'
    assert report['rows'] == []
    assert report['effective'] == 0

  def test_exact_50ms_boundary_and_invalid_values(self):
    collector = self.collector()
    self.update(collector, [])
    self.now[0] = 16
    report = self.update(collector, [(15.0, 50000, 50000), (15.05, 51000, 50500), (15.1, 2000, float('nan')), (15.2, True, 2000)])
    assert report['rows'][0]['n'] == 2
    assert report['rows'][0]['over50'] == 1
    assert report['rows'][0]['server_over50'] == 1

  def test_gap_restarts_warmup_instead_of_presenting_a_continuous_trial(self):
    collector = self.collector()
    self.update(collector, [])
    self.now[0] = 16
    self.update(collector, [(15.0, 26000, 25000)])
    self.now[0] = 25
    report = self.update(collector, [(24.0, 26000, 25000)])
    assert report['state'] == 'warmup'
    assert report['rows'] == []

  def test_memory_and_published_summaries_stay_bounded(self):
    collector = self.collector()
    report = self.ready(collector, 0.0, count=MAX_SAMPLES + 200)
    assert len(collector.samples) == MAX_SAMPLES
    assert report['rows'][0]['n'] == MAX_SAMPLES
    assert self.path.stat().st_size < 16384
    assert 'samples' not in json.loads(self.path.read_text())

  def test_untrusted_published_fields_cannot_create_a_winner(self):
    assert quality_status({'version': 1, 'active': True, 'effective': 0, 'state': 'ready', 'rows': []}) is None
    collector = self.collector()
    report = self.ready(collector, 0.0)
    report['best'] = 0
    assert quality_status(report)['best'] is None
    report['rows'][0]['p99_ms'] = float('nan')
    assert quality_status(report) is None


class TestServerWindow:
  def test_optional_raw_tail_is_bounded_and_legacy_reply_is_unchanged(self):
    source = ROOT / 'third_party/jetlink/jetlink/server/session.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'FrameStats')
    now = [100.0]
    namespace = {'deque': deque, 'time': NS(perf_counter=lambda: now[0])}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    frames = namespace['FrameStats']()
    frames.samples.extend((95 + i * 0.01, 30000, 25000) for i in range(200))
    before = list(frames.samples)
    legacy = frames.display_summary()
    raw = frames.display_summary(include_samples=True)
    assert 'samples' not in legacy
    assert len(raw.pop('samples')) == 64
    assert raw == legacy
    assert list(frames.samples) == before
