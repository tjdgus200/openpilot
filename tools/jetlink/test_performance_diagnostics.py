"""Production instrumentation is off without changing timing guards or outputs."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('value,enabled', [(None, False), ('0', False), ('1', True), ('true', False)])
def test_only_explicit_private_opt_in_enables_diagnostics(value, enabled):
  env = os.environ.copy()
  env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(ROOT / 'third_party/jetlink'), env.get('PYTHONPATH'))))
  env.pop('CARROT_PERF_DIAGNOSTICS', None)
  if value is not None:
    env['CARROT_PERF_DIAGNOSTICS'] = value
  code = ('from openpilot.common.performance_diagnostics import ENABLED; ' +
          'from jetlink.server.session import PERF_DIAGNOSTICS; ' +
          f'assert ENABLED is {enabled}; assert PERF_DIAGNOSTICS is {enabled}')
  subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, check=True, timeout=20)


def test_disabled_runtime_does_not_sample_proc_clock_or_log(monkeypatch):
  from openpilot.common import runtime_diagnostics as runtime
  def forbidden(*args, **kwargs):
    raise AssertionError('performance instrumentation ran')
  monkeypatch.setattr(runtime.time, 'monotonic', forbidden)
  monkeypatch.setattr(runtime.Path, 'read_text', forbidden)
  collector = runtime.RuntimeDiagnostics('test', forbidden, enabled=False)
  for _ in range(1000):
    collector.record(inference_ms=25.)
  assert vars(collector) == {'enabled': False}


def test_no_stats_thread_or_samples_but_qualified_park_switch_still_works(monkeypatch):
  import input_fusion as fusion
  import threading
  monkeypatch.setattr(threading.Thread, 'start', lambda _: pytest.fail('stats worker started'))
  collector = fusion.Comparison(clock=lambda: 10., measure=False)
  engine = NS(image_fusion=NS(qualified=True, active=1))
  changes = []
  def select(mode):
    engine.image_fusion.active = mode
    changes.append(mode)
  engine.image_fusion.select = select
  loaded = NS(engine=engine)
  collector.before(loaded)
  assert changes == []  # Park admission still required.
  collector.observe(b'{"input_fusion":{"mode":0,"parked":true}}', 10.)
  for _ in range(1000):
    collector.before(loaded)
    collector.record(1, 2)
  assert changes and set(changes) == {0}
  assert collector.rows == () and collector.thread is None
  assert collector.value['effective'] == 0 and collector.value['change_allowed']
  assert collector.report()['measurement_enabled'] is False
  collector.clear(10.)
  collector.before(loaded)
  assert collector.value['effective'] == 0 and collector.value['change_allowed']
  collector.park_until = 9.
  assert collector.report()['change_allowed'] is False
  collector.close()


def test_hud_uses_only_the_shared_resource_and_inference_sampler(monkeypatch):
  import hud_health as hud
  import hud_runtime_stats as stats
  monkeypatch.setattr(hud, 'PERF_DIAGNOSTICS', False)
  created = []
  def sampler(*, timing_enabled, compare_quality):
    created.append((timing_enabled, compare_quality))
    return NS(sample=lambda: {})
  monkeypatch.setattr(stats, 'RuntimeFooterSampler', sampler)
  renderer = object.__new__(hud.HealthRenderer)
  renderer.language = 'en'
  renderer._system_stats = NS(sample=lambda: NS(cpu_used_percent=12., memory_used_percent=49.))
  drawn = []
  renderer._draw_runtime_line = lambda *args: drawn.append(args)
  assert renderer._host_metrics() == {}
  renderer._draw_cluster_core_usage('')
  assert created == [(True, False)]
  assert len(drawn) == 1 and drawn[0][-1] == 'resources'
  text = ''.join(segment[0] for segment in drawn[0][0])
  assert 'CPU' in text and 'MEM' in text
  assert all(label not in text for label in ('FPS', 'P99', 'AVG', 'INFER'))


def test_transport_keeps_packet_identical_without_tx_observation(monkeypatch):
  from jetlink.transport.base import StreamTransport
  from jetlink import protocol as protocol
  class Transport(StreamTransport):
    def _write(self, parts):
      self.wire = b''.join(parts)
      return len(self.wire)
    def _read_into(self, *args):
      raise AssertionError('unexpected read')
    def close(self):
      pass
  plain, measured = Transport(), Transport()
  measured.measure_send_ready = True
  for transport in (plain, measured):
    transport.send(protocol.Msg.INFER_RESP, 1, (b'unchanged-output',))
  assert plain.wire == measured.wire
  assert not hasattr(plain, 'last_send_ready') and measured.last_send_ready[1] == 1


@pytest.mark.parametrize('enabled,hud_timing', [(False, False), (True, False), (False, True)])
def test_actual_session_feedback_and_outputs_identical_with_timing(monkeypatch, enabled, hud_timing):
  import numpy as np
  from jetlink.server import session
  from test_output_projection import pair, infer
  monkeypatch.setattr(session, 'PERF_DIAGNOSTICS', enabled)
  client, server, loaded, outputs = pair()
  server.hud_inference_timing = hud_timing
  server.host.frame_stats = session.FrameStats()
  rng = np.random.default_rng(211)
  for frame in range(8):
    for array in outputs.values():
      array[...] = rng.uniform(-1, 1, array.shape)
    result = infer(client, frame, reset=frame == 0)
    np.testing.assert_array_equal(result, outputs['outputs'].reshape(-1))
    for name, source in loaded.spec.state_pairs.items():
      assert loaded.host_inputs[name].tobytes() == outputs[source].tobytes()
  assert len(server.host.frame_stats.samples) == (8 if enabled or hud_timing else 0)
  if hud_timing:
    from jetlink.transport.base import LinkError
    outputs['outputs'].flat[0] = np.nan
    with pytest.raises(LinkError):
      infer(client, 9)
    assert len(server.host.frame_stats.samples) == 8
