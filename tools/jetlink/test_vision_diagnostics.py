"""Classified failures over actual local/encrypted sockets; no vehicle access."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import aux_vision
import vision_cpu as worker
from hud_wifi_route import select_link
from openpilot.selfdrive.carrot.xiaoge import remote as r
from test_vision_cpu import request


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
  monkeypatch.setattr(r, 'SESSION_STATUS', tmp_path / 'session.json')
  monkeypatch.setattr(r, 'SOCKET', str(tmp_path / 'rpc'))


@pytest.mark.parametrize('peer,code', [
  ({'carrot_host': 'jetson'}, 'runtime_outdated'),
  ({'carrot_host': 'jetson', r.CAPABILITY: False}, 'runtime_incomplete'),
  ({'carrot_host': 'jetson', r.CAPABILITY: False, 'carrot_aux_cpu_error': 'dependency_missing'}, 'dependency_missing'),
  ({'carrot_host': 'jetson', r.CAPABILITY: False, 'carrot_aux_cpu_error': 'model_missing'}, 'model_missing'),
  ({'carrot_host': 'jetson', r.CAPABILITY: False, 'carrot_aux_cpu_error': 'secret exception'}, 'runtime_incomplete'),
])
def test_old_runtime_is_distinct_from_incomplete_install_and_has_no_secret(peer, code):
  peer.update(private_key='secret-test-string', carrot_source_commit='f' * 40)
  assert aux_vision.publisher(None, peer) is None
  diagnostic = r.CpuClient('lane', 'unused').diagnostic()
  assert diagnostic['code'] == code and diagnostic['sourceCommit'] == 'f' * 40
  assert 'secret' not in r.SESSION_STATUS.read_text()
  aux_vision.end_session()
  assert r.session_diagnostic()['code'] == 'disconnected'


@pytest.mark.parametrize('code', ['wifi_route_unavailable', 'authentication_failed', 'model_load_failed',
                                  'model_hash_mismatch', 'inference_failed', 'response_timeout', 'remote_closed'])
def test_local_error_roundtrip_preserves_code_and_attempts_are_not_successes(tmp_path, monkeypatch, code):
  path = tmp_path / 'identity'
  path.write_bytes(b'model identity')
  client = r.CpuClient('lane', path)
  assert client.load()
  runtime = worker.Runtime('send', bytes(32), None)
  def fail(_data):
    raise r.RemoteFailure(code)
  monkeypatch.setattr(runtime, 'relay', fail)
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(r.SOCKET)
  listener.listen(1)
  def serve():
    with listener:
      sock, _ = listener.accept()
      runtime.handle(sock)
  thread = threading.Thread(target=serve)
  thread.start()
  try:
    with pytest.raises(r.RemoteFailure) as error:
      client.infer(np.zeros(r.SHAPES['lane'], np.uint8))
    assert error.value.code == code
    status = client.diagnostic()
    assert status['code'] == code and status['attempts'] == status['failures'] == 1
    assert status['successes'] == 0 and not client.remote_ready()
  finally:
    thread.join(2)
  assert not thread.is_alive()


@pytest.mark.parametrize('known', [True, False])
def test_host_error_is_authenticated_sanitized_and_does_not_become_empty_lane(known):
  a, b = socket.socketpair()
  runtime = worker.Runtime('receive', bytes(32), None)
  def fail(_data):
    if known:
      raise r.RemoteFailure('model_hash_mismatch')
    raise RuntimeError('SECRET_TEST_STRING /private/path private-key')
  runtime.engine = NS(infer=fail, expire=lambda: None)
  def serve():
    try:
      with b:
        runtime.receive(b)
    except ConnectionError:
      pass
  thread = threading.Thread(target=serve)
  thread.start()
  try:
    with a:
      _, key = worker.authenticate(a, runtime.key, 1)
      header = r.PREFIX.pack(1)
      data = request()
      r.send_record(a, header + worker.seal(key, 1, 0, header, data), time.monotonic() + 1)
      encrypted = r.read_record(a, r.MAX_RESPONSE, time.monotonic() + 1)
      response = worker.unseal(key, 1, 1, header, encrypted)
      value = json.loads(response)
      assert value['id'] == 'a' * 32 and value['sha256'] == 'b' * 64
      assert value['valid'] is False and 'leftLine' not in value
      assert value['errorCode'] == ('model_hash_mismatch' if known else 'inference_failed')
      assert value['origin'] == 'jetson' and b'SECRET' not in response and b'/private' not in response
  finally:
    thread.join(2)
  assert not thread.is_alive()


@pytest.mark.parametrize('local,targets,code', [([], ['172.29.77.2'], 'wifi_local_unavailable'),
                                            ([('wlan0', '10.1.1.1')], [], 'wifi_peer_unavailable')])
def test_missing_local_and_peer_addresses_are_distinguished(monkeypatch, local, targets, code):
  runtime = worker.Runtime('send', bytes(32), None)
  monkeypatch.setattr(worker, 'wlan_addresses', lambda: local)
  monkeypatch.setattr(worker, 'target_addresses', lambda: targets)
  with pytest.raises(r.RemoteFailure) as error:
    runtime.relay(request())
  assert error.value.code == code


@pytest.mark.parametrize('phase,error,code', [('connect', TimeoutError, 'wifi_connect_timeout'),
                                          ('connect', ConnectionRefusedError, 'wifi_connect_failed'),
                                          ('auth', ValueError, 'authentication_failed'),
                                          ('auth', TimeoutError, 'authentication_timeout'),
                                          ('auth', ConnectionError, 'authentication_unavailable')])
def test_connect_failure_and_bad_authentication_are_not_inference_failures(monkeypatch, phase, error, code):
  class Socket:
    closed = False
    def settimeout(self, *_): pass
    def setsockopt(self, *_): pass
    def bind(self, *_): pass
    def connect(self, *_):
      if phase == 'connect':
        raise error('SECRET_TEST_STRING')
    def close(self): self.closed = True
  sock = Socket()
  monkeypatch.setattr(worker, 'wlan_addresses', lambda: [('wljetson0', '172.29.77.1')])
  monkeypatch.setattr(worker, 'target_addresses', lambda: ['172.29.77.2'])
  monkeypatch.setattr(worker, 'select_link', lambda *_: ('wljetson0', '172.29.77.1', '172.29.77.2'))
  monkeypatch.setattr(worker.socket, 'socket', lambda *_: sock)
  def auth(*_): raise error('SECRET_TEST_STRING')
  monkeypatch.setattr(worker, 'authenticate', auth)
  runtime = worker.Runtime('send', bytes(32), None)
  with pytest.raises(r.RemoteFailure) as result:
    runtime.relay(request())
  assert result.value.code == code and 'SECRET' not in str(result.value) and sock.closed


def test_direct_ap_is_preferred_but_shared_wifi_remains_a_valid_route():
  network = [('wlan0', '10.1.1.41'), ('wljetson0', '172.29.77.1')]
  def route(target, source):
    return [{'dev': 'wljetson0' if target.startswith('172.') else 'wlan0'}]
  assert select_link(network, ['10.1.1.121', '172.29.77.2'], route)[0] == 'wljetson0'
  assert select_link(network, ['10.1.1.121'], route)[0] == 'wlan0'
  assert select_link(network, ['172.29.77.2'], lambda *_: [{'dev': 'usb0'}]) is None


def test_stopped_session_owner_does_not_keep_an_old_runtime_reason(monkeypatch):
  r.write_session('runtime_outdated')
  def gone(*_): raise ProcessLookupError()
  monkeypatch.setattr(os, 'kill', gone)
  assert r.session_diagnostic()['code'] == 'disconnected'


def test_web_diagnostics_render_distinct_failures_and_never_call_attempts_successes():
  node = shutil.which('node')
  if node is None:
    pytest.skip('Node unavailable for web diagnostics contract')
  path = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/carrot/xiaoge/v_asm_web.html'
  html = path.read_text()
  script = re.search(r'<script>([\s\S]+)</script>', html).group(1)
  declarations = script[script.index('const remoteReasons='):script.index('function errorMessage(')]
  checks = r'''
const assert=require('node:assert/strict');
let language='ko';
const element={textContent:'',className:''};
for(const code of ['runtime_outdated','wifi_route_unavailable','authentication_failed','inference_failed']){
  renderRemoteDiagnostic(element,{code,message:'safe',attempts:197,successes:0,wifiPath:'shared_wifi'},'차선');
  assert.ok(element.textContent.includes(code));
  assert.ok(element.textContent.includes('성공 0 / 요청 197'));
  assert.ok(element.textContent.includes('공유기/핫스팟'));
  assert.equal(element.className,'bad');
}
renderRemoteDiagnostic(element,{code:'ok',message:'ready',attempts:1,successes:1,wifiPath:'direct_ap'},'차선');
assert.ok(element.textContent.includes('직접 AP'));
assert.equal(element.className,'ok');
for(const locale of ['en','zh']){
  language=locale;
  renderRemoteDiagnostic(element,{code:'authentication_failed',message:'Authentication failed',wifiPath:'unknown'},'BSD');
  assert.ok(element.textContent.includes('authentication_failed'));
}
'''
  # Parse the entire shipped script, then exercise the actual diagnostic renderer.
  code = 'new (require("node:vm").Script)(' + json.dumps(script) + ');\n' + declarations + checks
  result = subprocess.run([node, '-'], input=code, text=True, encoding='utf-8', capture_output=True, timeout=15)
  assert result.returncode == 0, result.stderr
