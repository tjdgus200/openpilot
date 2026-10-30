"""CPU-only gate/timing/ownership/qualification regressions; no device access."""
import ctypes
import importlib.util
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from input_fusion import Comparison, gate, selection
from openpilot.common.input_fusion_status import status
from test_device_state import runtime as runtime, make_engine  # explicit pytest fixture re-export


def packet(mode=1, parked=True, fps=10):
  return json.dumps({'input_fusion': {'mode': mode, 'parked': parked, 'hud_fps': fps}}).encode()


def setup_compare():
  now = [0.]
  c = Comparison(clock=lambda: now[0], worker=False)
  changes = []
  f = NS(active=1, qualified=True)
  def select_mode(mode):
    changes.append(mode)
    f.active = mode
  f.select = select_mode
  loaded = NS(engine=NS(image_fusion=f))
  return c, now, loaded, changes


def test_switch_requires_fresh_park_and_freezes_otherwise():
  c, now, loaded, changes = setup_compare()
  c.before(loaded)
  assert not changes
  c.observe(packet(0, False), 0)
  c.before(loaded)
  assert not changes and c.effective == 1
  now[0] = .3
  c.observe(packet(0), .3)
  c.before(loaded)
  assert changes == [0] and c.effective == 0
  now[0] = 2
  c.observe(packet(1), .3)  # stale queued HUD cannot authorize a switch
  c.before(loaded)
  assert changes == [0]


def test_samples_exclude_warmup_retain_other_arm_and_reset_on_model_fps():
  c, now, loaded, _ = setup_compare()
  c.observe(packet(0), 0)
  c.before(loaded)
  c.record(1, 2)
  assert not c.rows[0]
  for i in range(1221):
    now[0] = 15 + i * .05
    c.park_until = now[0] + 1
    start = round(now[0] * 1e9)
    c.record(start, start + (51_000_000 if i == 1220 else 30_000_000))
  r = c.report()
  assert r['state'] == 'ready' and r['rows'][0]['n'] == 1221
  assert r['rows'][0]['over50'] == 1 and status(r)
  now[0] += 1
  c.observe(packet(1), now[0])
  c.before(loaded)
  assert len(c.rows[0]) == 1221 and not c.rows[1]
  now[0] += .3
  c.observe(packet(1, fps=20), now[0])
  assert not c.rows[0] and not c.rows[1]
  c.rows[0].append((now[0], 25))
  c.before(NS(engine=loaded.engine))
  assert not c.rows[0]


def test_bounded_history_and_lost_park_restarts_current_arm():
  c, now, loaded, _ = setup_compare()
  c.observe(packet(), 0)
  c.before(loaded)
  for i in range(6500):
    now[0] = 15 + i * .05
    c.park_until = now[0] + 1
    c.record(round(now[0] * 1e9), round(now[0] * 1e9) + 25_000_000)
  assert len(c.rows[1]) == 6000
  c.park_until = -1
  c.before(loaded)
  c.observe(packet(), now[0])
  c.before(loaded)
  assert not c.rows[1] and c.report()['state'] == 'warmup'


@pytest.mark.parametrize('bad', [b'not-json', packet(True), packet(2), b'{}'])
def test_bad_control_never_authorizes_switch(bad):
  c, _, loaded, changes = setup_compare()
  c.observe(bad, 0)
  c.before(loaded)
  assert not changes


def test_complete_tx_measurement_does_not_include_usb_write_delay():
  from jetlink.transport.base import StreamTransport
  from jetlink import protocol as P
  class Transport(StreamTransport):
    def _write(self, parts):
      assert self.last_send_ready[:2] == (P.Msg.INFER_RESP, 7)
      self.wire = b''.join(parts)
      self.ready = self.last_send_ready[2]
      return len(self.wire)
    def _read_into(self, dest, timeout):
      raise AssertionError('read not expected')
    def close(self):
      pass
  t = Transport()
  t.send(P.Msg.INFER_RESP, 7, (b'test',))
  assert t.ready > 0 and t.wire.endswith(b'test')
  c, now, loaded, _ = setup_compare()
  c.observe(packet(), 0)
  c.before(loaded)
  now[0], c.park_until = 16., 17.
  c.record(16_000_000_000, 16_025_000_000)
  assert c.report()['rows'][0]['mean_ms'] == 25


def test_session_records_complete_response_before_slow_usb_and_counts_invalid(monkeypatch):
  import threading
  from jetlink import protocol as P
  from jetlink.transport.base import StreamTransport
  from server import CarrotSession
  import jetlink.transport.base as wire
  now = [16_025_000_000]
  monkeypatch.setattr(wire.time, 'monotonic_ns', lambda: now[0])
  class Transport(StreamTransport):
    def _write(self, parts):
      now[0] += 200_000_000  # slow send must not become part of the result
      return sum(len(p) for p in parts)
    def _read_into(self, *args):
      raise AssertionError('no receive')
    def close(self):
      pass
  session = CarrotSession.__new__(CarrotSession)
  comparison, clock, loaded, _ = setup_compare()
  comparison.observe(packet(), 0)
  comparison.before(loaded)
  clock[0], comparison.park_until = 16., 17.
  session.t, session.send_lock = Transport(), threading.Lock()
  session.telemetry = NS(health=None)
  session.input_comparison = comparison
  session._input_measurement = 16_000_000_000
  session._send(P.Msg.INFER_RESP, 1, (P.pack_infer_resp(1, P.Status.OK, 1, 2, 3), b'output'))
  assert comparison.report()['rows'][0]['mean_ms'] == 25
  session._send(P.Msg.INFER_RESP, 2, (P.pack_infer_resp(2, P.Status.NOT_FINITE, 1, 2, 3),))
  assert comparison.failed == 1 and len(comparison.rows[1]) == 1


def test_fresh_vehicle_gate_and_default():
  class SM(dict):
    valid = dict.fromkeys(('carState', 'carControl', 'selfdriveState'), True)
    alive = dict(valid)
    recv_time = dict.fromkeys(valid, 9.9)
  sm = SM(carState=NS(gearShifter='park', standstill=True, vEgo=0),
          carControl=NS(enabled=False), selfdriveState=NS(active=False))
  assert gate(sm, 10)
  assert not gate(sm, 11)
  sm['carState'].gearShifter = 'drive'
  assert not gate(sm, 10)
  assert selection(None) == 1 and selection(b'0') == 0 and selection(b'1') == 1


def test_saved_default_is_registered_and_reaches_existing_hud_control_channel():
  from pathlib import Path
  import hud_protocol
  root = Path(__file__).resolve().parents[2]
  catalog = json.loads((root / 'openpilot/selfdrive/carrot_settings.json').read_text(encoding='utf-8'))
  entries = catalog['params'] if 'params' in catalog else catalog['settings']
  setting = next(item for item in entries if item['name'] == 'JetsonInputFusion')
  assert (setting['default'], setting['min'], setting['max'], setting['control']) == (1, 0, 1, 'select')
  assert 'JetsonInputFusion' in hud_protocol.PARAMS
  assert '{"JetsonInputFusion", {PERSISTENT, INT, "1"}}' in (root / 'openpilot/common/params_keys.h').read_text()


def bridge(runtime, engine, queues, *, corrupt=False):
  # Each fake runtime owns its allocation table; never reuse a prior module's
  # cudart binding across tests.
  spec = importlib.util.spec_from_file_location('test_image_fusion', runtime.engine.__file__.replace('engine.py', 'image_fusion.py'))
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  f = module.ImageFusion.__new__(module.ImageFusion)
  f.engine, f.spec = engine, queues.spec
  f.active, f.qualified, f.graphs, f.cpu_cast = 0, False, {}, None
  f.host = f.device = runtime.cuda.host_alloc(4)
  f.u8 = runtime.engine._np_from_ptr(f.host, (2, 2), np.uint8)
  f.count, f.module, f.function = 4, None, 1
  def launch(function, gx, gy, gz, bx, by, bz, shared, stream, args, extra):
    pointers = (ctypes.c_void_p * 3).from_address(args)
    source, target, count = [ctypes.c_uint64.from_address(p).value for p in pointers]
    def convert():
      value = np.frombuffer(runtime.cuda.memory[source], np.uint8, count).astype(np.float16)
      if corrupt:
        value[0] += 1
      ctypes.memmove(target, value.ctypes.data, count * 2)
    runtime.cuda.enqueue('exact-cast', convert)
    return (0,)
  f.driver = NS(cuLaunchKernel=launch)
  engine.image_fusion = f
  return f


def test_default_attempt_requires_exact_n16_orin_and_device_recurrence(runtime, monkeypatch):
  from pathlib import Path
  from jetlink.spec import ModelSpec
  engine, queues, _ = make_engine(runtime, True, monkeypatch)
  fusion = bridge(runtime, engine, queues)
  path = Path(runtime.engine.__file__).with_name('image_fusion.py')
  spec = importlib.util.spec_from_file_location('fusion_gate', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  model_dir = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/modeld/model_catalog'
  called = []
  monkeypatch.delenv('CARROT_TRT_IMAGE_FUSION', raising=False)
  monkeypatch.setattr(module, 'ImageFusion', lambda *args: called.append(True) or fusion)
  real_file = module.Path.is_file
  monkeypatch.setattr(module.Path, 'is_file', lambda p: p.as_posix() == '/etc/nv_tegra_release' or real_file(p))
  try:
    for name in ('md-v3', 'ctv3m'):
      model = ModelSpec.from_dict(json.loads((model_dir / f'{name}.json').read_text()))
      assert module.configure(engine, model) is None and not called
    model = ModelSpec.from_dict(json.loads((model_dir / 'n16.json').read_text()))
    assert module.configure(engine, model) is fusion and called == [True]
    monkeypatch.setenv('CARROT_TRT_IMAGE_FUSION', '0')
    assert module.configure(engine, model) is None and called == [True]
    assert engine.image_fusion_reason == 'disabled'
  finally:
    engine.close()
  assert not runtime.cuda.memory


def test_prepare_failure_restores_baseline_before_serving(runtime, monkeypatch):
  from jetlink.server.session import EngineHost
  import sys
  engine, queues, _ = make_engine(runtime, True, monkeypatch)
  f = bridge(runtime, engine, queues, corrupt=True)
  module = sys.modules['jetlink.server.backends.trt']
  def configure(*args):
    engine.image_fusion = f
    return f
  monkeypatch.setattr(module, 'image_fusion', NS(configure=configure), raising=False)
  monkeypatch.setitem(sys.modules, 'jetlink.server.backends.trt.image_fusion', module.image_fusion)
  engine.image_fusion = None
  engine.device_state.close()
  engine.device_state = None
  queues.spec.sha256 = 'unsupported-test-spec'
  try:
    loaded = EngineHost._warm(None, engine, queues.spec)
    assert engine.image_fusion is None and engine.image_fusion_reason == 'probe_failed'
    assert engine.graph_exec is not None and f.host is None and not f.graphs
    loaded.queues.step_into(np.ones((2, 2), np.uint8), np.zeros(2, np.float32), loaded.host_inputs)
    loaded.queues.feedback(engine.run())
  finally:
    engine.close()
  assert not runtime.cuda.pending and not runtime.cuda.memory


def test_two_fixed_graphs_qualify_then_switch_with_recurrence_and_resets(runtime, monkeypatch):
  engine, queues, inputs = make_engine(runtime, True, monkeypatch)
  f = bridge(runtime, engine, queues)
  try:
    engine.warm()
    f.qualify(queues, inputs)
    assert f.qualified and f.active == 1 and set(f.graphs) == {0, 1}
    for frame in range(32):
      if frame in (3, 15, 25):
        queues.reset()
      f.select(frame % 2)
      pixels = np.full((2, 2), frame * 7, np.uint8)
      queues.step_into(pixels, np.array([frame, 0], np.float32), inputs)
      out = engine.run()
      queues.feedback(out)
      b = engine.inputs['new_img']
      assert bytes(runtime.cuda.memory[b.device_ptr])[:b.nbytes] == pixels.astype(np.float16).tobytes()
  finally:
    engine.close()
  assert not runtime.cuda.pending and not runtime.cuda.memory


def test_bad_gpu_cast_refuses_qualification_and_preserves_cpu_graph(runtime, monkeypatch):
  engine, queues, inputs = make_engine(runtime, True, monkeypatch)
  f = bridge(runtime, engine, queues, corrupt=True)
  try:
    engine.warm()
    with pytest.raises(ValueError, match='qualification'):
      f.qualify(queues, inputs)
    baseline = f.graphs.pop(0)
    f.close(keep_graph=baseline)
    engine.image_fusion = None
    engine.graph_exec = baseline
    queues.input_cast = f.cpu_cast
    queues.reset()
    queues.step_into(np.ones((2, 2), np.uint8), np.zeros(2, np.float32), inputs)
    queues.feedback(engine.run())
  finally:
    engine.close()
  assert not runtime.cuda.pending and not runtime.cuda.memory


def test_validator_drops_malformed_metrics_not_credentials():
  c, _, _, _ = setup_compare()
  value = c.report()
  value['reason'] = 'arbitrary-private-text'
  assert status(value)['reason'] == 'unsupported'
  value['rows'] = [{'id': 0, 'n': 1, 'seconds': 1, 'mean_ms': float('nan')}]
  assert status(value) is None
