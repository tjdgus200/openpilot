"""Real CPU ONNX and authenticated lossless RPC checks; no vehicle/GPU claim."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock  # noqa: TID251 -- pytest tests; mock transport only

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
import aux_vision as policy
import vision_cpu as worker
from openpilot.selfdrive.carrot.xiaoge import remote as r


@pytest.fixture(autouse=True)
def isolated_session_status(tmp_path, monkeypatch):
  monkeypatch.setattr(r, 'SESSION_STATUS', tmp_path / 'session.json')


def request(kind='lane', image=None, **extra):
  meta = {'version': 1, 'id': 'a' * 32, 'kind': kind, 'threshold': .25, 'sha256': 'b' * 64, 'op': 'infer'}
  meta.update(extra)
  data = json.dumps(meta).encode()
  pixels = bytes(np.prod(r.SHAPES[kind])) if image is None else image.tobytes()
  if meta['op'] == 'unload':
    pixels = b''
  return r.PREFIX.pack(len(data)) + data + pixels


@pytest.mark.parametrize('kind', ['lane', 'vasm'])
def test_lossless_request_size_and_shape(kind):
  image = np.arange(np.prod(r.SHAPES[kind]), dtype=np.uint8).reshape(r.SHAPES[kind])
  data = request(kind, image)
  meta, pixels = r.decode_request(data)
  assert meta['kind'] == kind and len(data) < r.MAX_REQUEST
  assert pixels == image.tobytes()


@pytest.mark.parametrize('change', [{'threshold': True}, {'threshold': float('nan')},
                                   {'threshold': -1}, {'id': 'x' * 32}, {'version': True},
                                   {'version': 2}, {'sha256': 'z' * 64}, {'op': 'unknown'}, {'extra': 1}])
def test_invalid_metadata_is_rejected(change):
  with pytest.raises(ValueError):
    r.decode_request(request(**change))


def test_lengths_are_checked_before_allocating_large_frames():
  for data in (request()[:-1], request() + b'\0', b'bad'):
    with pytest.raises(ValueError):
      r.decode_request(data)
  a, b = socket.socketpair()
  with a, b:
    a.sendall(r.PREFIX.pack(2**31))
    with pytest.raises(ValueError):
      r.read_record(b, r.MAX_REQUEST, time.monotonic() + .1)


@pytest.mark.parametrize('corruption', ['id', 'sha256', 'processor', 'valid', 'error_state', 'list', None])
def test_client_matches_request_model_processor_and_local_deadline(tmp_path, monkeypatch, corruption):
  monkeypatch.setattr(r, 'SOCKET', str(tmp_path / 'rpc'))
  path = tmp_path / 'synthetic-model'
  path.write_bytes(b'not model weights')
  client = r.CpuClient('lane', path)
  assert client.load()
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(r.SOCKET)
  listener.listen(1)
  def answer():
    with listener:
      sock, _ = listener.accept()
      with sock:
        meta, pixels = r.decode_request(r.read_record(sock, r.MAX_REQUEST, time.monotonic() + 1))
        assert pixels == bytes(416 * 416)
        result = {'id': meta['id'], 'sha256': client.sha256, 'processor': 'jetson_cpu_wifi', 'valid': True,
                  'leftLine': 0, 'rightLine': 1, 'leftConf': .9, 'rightConf': .8}
        if corruption in ('id', 'sha256', 'processor'):
          result[corruption] = 'wrong'
        if corruption == 'valid':
          result['valid'] = False
        if corruption == 'error_state':
          result.update(valid=False, errorCode='ok')
        if corruption == 'list':
          result = []
        r.send_record(sock, json.dumps(result).encode(), time.monotonic() + 1)
  thread = threading.Thread(target=answer)
  thread.start()
  try:
    if corruption is None:
      assert client.infer(np.zeros((416, 416), np.uint8))['valid']
      assert client.remote_ready()
    else:
      with pytest.raises(ValueError):
        client.infer(np.zeros((416, 416), np.uint8))
      assert not client.remote_ready()
  finally:
    thread.join(2)
  assert not thread.is_alive()


@pytest.mark.parametrize('kind', ['lane', 'vasm'])
@pytest.mark.parametrize('pattern', [0, 1])
def test_actual_cpu_models_match_the_lossless_remote_input(kind, pattern):
  from openpilot.selfdrive.carrot.xiaoge.lane_inference import LaneInference
  from openpilot.selfdrive.carrot.xiaoge.v_asm_inference import VASMInference
  path = ROOT / 'openpilot/selfdrive/carrot/xiaoge/assets' / r.FILES[kind]
  image = np.zeros(r.SHAPES[kind], np.uint8) if pattern == 0 else np.arange(
    np.prod(r.SHAPES[kind]), dtype=np.uint8).reshape(r.SHAPES[kind])
  reference = (LaneInference if kind == 'lane' else VASMInference)(path)
  assert reference.load(), reference.error
  expected = reference.infer(image, 416, 416) if kind == 'lane' else {'confidence': reference.predict_square(image)}
  engine = worker.Engine()
  actual = json.loads(engine.infer(request(kind, image, sha256=hashlib.sha256(path.read_bytes()).hexdigest())))
  assert actual['valid'] and actual['processor'] == 'jetson_cpu_wifi'
  for key in (('leftLine', 'rightLine', 'leftConf', 'rightConf', 'candidatesCount') if kind == 'lane' else ('confidence',)):
    assert actual[key] == pytest.approx(expected[key], abs=1e-6)
  with pytest.raises(r.RemoteFailure) as error:
    engine.infer(request(kind, image))
  assert error.value.code == 'model_hash_mismatch'


def test_host_unloads_disabled_and_idle_models(monkeypatch):
  engine = worker.Engine()
  engine.models = {'lane': object(), 'vasm': object()}
  engine.used = {'lane': 9., 'vasm': 6.}
  monkeypatch.setattr(worker.time, 'monotonic', lambda: 10.)
  engine.expire()
  assert set(engine.models) == {'lane'}
  result = json.loads(engine.infer(request(op='unload')))
  assert result['valid'] and not engine.models


@pytest.mark.parametrize('corrupt', ['replay', 'tamper', 'key'])
def test_real_authenticated_records_reject_replay_tamper_and_wrong_key(corrupt):
  a, b = socket.socketpair()
  runtime = worker.Runtime('receive', bytes(32), None)
  calls, errors = [], []
  runtime.engine = NS(infer=lambda data: calls.append(data) or b'{"valid":true}', expire=lambda: None)
  def serve():
    try:
      with b:
        runtime.receive(b)
    except (OSError, ValueError) as error:
      errors.append(type(error).__name__)
  thread = threading.Thread(target=serve)
  thread.start()
  try:
    with a:
      if corrupt == 'key':
        with pytest.raises((OSError, ValueError)):
          worker.authenticate(a, b'wrong'.ljust(32, b'0'), 1)
      else:
        _, key = worker.authenticate(a, runtime.key, 1)
        header = r.PREFIX.pack(1)
        frame = header + worker.seal(key, 1, 0, header, request())
        if corrupt == 'tamper':
          frame = frame[:-1] + bytes([frame[-1] ^ 1])
        r.send_record(a, frame, time.monotonic() + 1)
        if corrupt == 'replay':
          reply = r.read_record(a, r.MAX_RESPONSE, time.monotonic() + 1)
          assert worker.unseal(key, 1, 1, header, reply) == b'{"valid":true}'
          r.send_record(a, frame, time.monotonic() + 1)
  finally:
    thread.join(2)
  assert not thread.is_alive() and errors == (['ConnectionError'] if corrupt == 'key' else ['ValueError'])
  assert len(calls) == (1 if corrupt == 'replay' else 0)


def test_missing_wifi_never_opens_a_usb_or_ethernet_connection(monkeypatch):
  runtime = worker.Runtime('send', bytes(32), None)
  monkeypatch.setattr(worker, 'wlan_addresses', lambda: [('usb0', '192.168.60.1')])
  monkeypatch.setattr(worker, 'target_addresses', lambda: ['192.168.60.2'])
  monkeypatch.setattr(worker.socket, 'socket', Mock(side_effect=AssertionError('non-WLAN connection')))
  with pytest.raises(r.RemoteFailure) as error:
    runtime.relay(request())
  assert error.value.code == 'wifi_route_unavailable'


def test_silent_lost_peer_does_not_hold_receiver_forever(monkeypatch):
  monkeypatch.setattr(worker, 'IDLE_SECONDS', .03)
  a, b = socket.socketpair()
  runtime = worker.Runtime('receive', bytes(32), None)
  errors = []
  def serve():
    try:
      with b:
        runtime.receive(b)
    except TimeoutError:
      errors.append('expired')
  thread = threading.Thread(target=serve)
  thread.start()
  with a:
    worker.authenticate(a, runtime.key, 1)
    thread.join(.5)
  assert not thread.is_alive() and errors == ['expired']


def test_unknown_or_old_hosts_do_not_start_auxiliary_work(monkeypatch):
  monkeypatch.setattr(policy, 'Worker', Mock(side_effect=AssertionError('local fallback')))
  for peer in ({}, {'carrot_host': 'jetson'}, {'carrot_host': 'mac', policy.CAPABILITY: True}):
    assert policy.publisher(None, peer) is None


def test_usb_negotiation_contains_only_a_key_not_image_or_model_data(monkeypatch):
  events = []
  worker_instance = object()
  monkeypatch.setattr(policy, 'Worker', lambda *a, **kw: events.append((a, kw)) or worker_instance)
  client = NS(_next_seq=lambda: 1, t=NS(send_json=lambda *args: events.append(args)),
              _expect=lambda *args: NS(payload=b'{"ready":true}'))
  assert policy.publisher(client, {'carrot_host': 'jetson', policy.CAPABILITY: True}) is worker_instance
  assert events[0][:2] == (policy.REQUEST, 1) and set(events[0][2]) == {'key'}
  assert len(bytes.fromhex(events[0][2]['key'])) == 32
  assert events[1][1] == {'script': 'vision_cpu.py', 'port': 7717}


def test_receiver_key_setup_is_independent_of_hud_and_contains_no_images(monkeypatch):
  import server
  calls, replies = [], []
  session = object.__new__(server.CarrotSession)
  session.last_seq, session.aux_cpu_supported, session.aux_cpu = 0, True, None
  session.hud_wifi_supported = False
  session._send_json = lambda *args: replies.append(args)
  monkeypatch.setattr(server.aux_vision, 'receiver', lambda key: calls.append(key) or NS(close=lambda: None))
  message = NS(msg_type=policy.REQUEST, seq=1, payload=json.dumps({'key': 'a' * 64}).encode())
  session.handle(message)
  assert calls == ['a' * 64] and replies[-1] == (policy.RESPONSE, 1, {'ready': True})
  session.handle(message)
  assert len(calls) == 1  # No duplicate worker for replayed control setup.
  session.handle(NS(msg_type=policy.REQUEST, seq=2, payload=json.dumps({'key': 'a' * 64, 'image': 'x'}).encode()))
  assert replies[-1] == (policy.RESPONSE, 2, {'ready': False}) and len(calls) == 1


def test_auxiliary_setup_failure_does_not_invalidate_driving_setup(monkeypatch):
  client = NS(_next_seq=lambda: 1, t=NS(send_json=Mock(side_effect=OSError('unavailable'))))
  assert policy.publisher(client, {'carrot_host': 'jetson', policy.CAPABILITY: True}) is None


def test_session_never_negotiates_auxiliary_wifi_and_reaps_display_resources(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import daemon
  calls = []
  monkeypatch.setattr(daemon, 'display_publisher', lambda *a: NS(close=lambda: calls.append('hud')))
  monkeypatch.setattr(daemon, 'PhasePublisher', lambda: NS(close=lambda: calls.append('phase')))
  auxiliary = Mock(side_effect=AssertionError('Auxiliary Wi-Fi started'))
  monkeypatch.setattr(policy, 'publisher', auxiliary)
  client = NS(t=NS(send_json=Mock(side_effect=AssertionError('Wi-Fi handshake sent'))))
  def drive(*args):
    calls.append('drive')
    raise ConnectionError('session ended')
  monkeypatch.setattr(daemon, '_serve_local', drive)
  with pytest.raises(ConnectionError, match='session ended'):
    daemon.serve_local(None, client, {'carrot_host': 'jetson', policy.CAPABILITY: True})
  assert calls == ['drive', 'phase', 'hud']
  auxiliary.assert_not_called()
  client.t.send_json.assert_not_called()


def test_official_service_toggle_roi_and_preprocessing_regressions():
  env = {**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}
  result = subprocess.run([sys.executable, str(ROOT / 'tools/jetlink/check_aux_service.py')],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
  assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux worker scheduling')
@pytest.mark.parametrize(('role', 'inherited', 'expected'), [
  ('send', 0, 19), ('send', 19, 19), ('receive', 0, 10),
  ('receive', 15, 15), ('--check-models', 0, 10), ('--check-models', 19, 19),
])
def test_cpu_workers_never_raise_inherited_priority(monkeypatch, role, inherited, expected):
  calls = []
  for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'CUDA_VISIBLE_DEVICES'):
    monkeypatch.setenv(name, 'before')
  monkeypatch.setattr(os, 'sched_setscheduler', lambda *args: calls.append(('policy', args)))
  monkeypatch.setattr(os, 'getpriority', lambda *args: inherited)
  def setpriority(which, who, priority):
    if priority < inherited:
      raise PermissionError('Unprivileged priority increase')
    calls.append(('nice', priority))
  monkeypatch.setattr(os, 'setpriority', setpriority)
  monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: {0, 1, 2, 3})
  monkeypatch.setattr(os, 'sched_setaffinity', lambda *args: calls.append(('affinity', args)))
  worker.process_limits(role)
  assert calls == [('policy', (0, os.SCHED_OTHER, os.sched_param(0))),
                   ('nice', expected), ('affinity', (0, [0, 1]))]
  assert os.environ['CUDA_VISIBLE_DEVICES'] == '-1'
  assert os.environ['OPENBLAS_NUM_THREADS'] == os.environ['OMP_NUM_THREADS'] == '1'


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux nice/UID isolation')
def test_install_probe_retains_nice19_without_root_privileges():
  # Run the actual production function in a disposable child, not in pytest or
  # the device services. Reading/compiling it first also works in root-only CI
  # checkout directories, before the child irreversibly drops its own UID.
  script = '''
import ast, json, os, sys
from pathlib import Path
tree = ast.parse(Path(sys.argv[1]).read_text())
function = next(v for v in tree.body if isinstance(v, ast.FunctionDef) and v.name == 'process_limits')
namespace = {'os': os}
exec(compile(ast.Module(body=[function], type_ignores=[]), '<worker-limits>', 'exec'), namespace)
if os.geteuid() == 0:
  os.setgroups([])
  os.setgid(65534)
  os.setuid(65534)
os.setpriority(os.PRIO_PROCESS, 0, 19)
namespace['process_limits']('--check-models')
print(json.dumps({'nice': os.getpriority(os.PRIO_PROCESS, 0),
                  'policy': os.sched_getscheduler(0), 'unprivileged': os.geteuid() != 0}))
'''
  result = subprocess.run([sys.executable, '-c', script, str(ROOT / 'tools/jetlink/vision_cpu.py')],
                          capture_output=True, text=True, timeout=15)
  assert result.returncode == 0, result.stderr
  assert json.loads(result.stdout) == {'nice': 19, 'policy': os.SCHED_OTHER, 'unprivileged': True}
