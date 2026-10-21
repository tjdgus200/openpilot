"""Bounded local RPC to the session-authenticated WLAN worker; never run ONNX here."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import time
import re
import tempfile

SOCKET = '/dev/shm/carrot-aux-vision.sock'
PORT = 7717
CAPABILITY = 'carrot_aux_cpu_wifi_v1'
DIAGNOSTICS_CAPABILITY = 'carrot_aux_cpu_diagnostics_v1'
SESSION_STATUS = Path('/dev/shm/carrot-aux-vision-session.json')
DEADLINE = .75
MAX_REQUEST = 380_000
MAX_RESPONSE = 4096
PREFIX = struct.Struct('!I')
SHAPES = {'lane': (416, 416), 'vasm': (352, 352, 3)}
FILES = {'lane': 'lane.onnx', 'vasm': 'v_asm_model.onnx'}
MODEL_IDLE_SECONDS = 3.0

# Fixed messages only: never forward exception text, paths, packets or session keys.
ERRORS = {
  'ok': ('ready', 'Jetson CPU result received'),
  'awaiting_result': ('waiting', 'Waiting for a valid Jetson CPU result'),
  'disabled': ('disabled', 'Visual BSD is disabled'),
  'runtime_outdated': ('runtime', 'Jetson runtime lacks CPU/Wi-Fi support; update Jetson'),
  'runtime_incomplete': ('runtime', 'Jetson CPU/Wi-Fi support is unavailable; check runtime dependencies'),
  'host_unsupported': ('runtime', 'The connected host is not a supported Jetson'),
  'dependency_missing': ('model', 'Jetson CPU OpenCV dependency is unavailable'),
  'model_missing': ('model', 'Jetson auxiliary ONNX file is missing'),
  'model_load_failed': ('model', 'Jetson could not load the auxiliary ONNX model'),
  'model_hash_mismatch': ('model', 'Comma and Jetson auxiliary model hashes differ'),
  'local_model_missing': ('model', 'Comma auxiliary model identity file is unavailable'),
  'negotiating': ('session', 'Preparing the CPU/Wi-Fi session'),
  'session_ready': ('session', 'CPU/Wi-Fi relay started; waiting for a result'),
  'session_setup_rejected': ('session', 'Jetson rejected CPU/Wi-Fi session setup'),
  'session_setup_timeout': ('session', 'CPU/Wi-Fi USB session setup timed out'),
  'session_setup_failed': ('session', 'CPU/Wi-Fi USB session setup failed'),
  'relay_start_failed': ('relay', 'Comma CPU/Wi-Fi relay could not start'),
  'relay_unavailable': ('relay', 'Comma CPU/Wi-Fi relay is unavailable'),
  'relay_busy': ('relay', 'CPU/Wi-Fi relay is processing another request'),
  'disconnected': ('session', 'Jetson CPU/Wi-Fi session is disconnected'),
  'wifi_local_unavailable': ('wifi', 'Comma has no usable Wi-Fi address'),
  'wifi_peer_unavailable': ('wifi', 'Jetson has no fresh advertised Wi-Fi address'),
  'wifi_route_unavailable': ('wifi', 'No Wi-Fi route to Jetson; USB/Ethernet fallback is disabled'),
  'wifi_connect_timeout': ('wifi', 'Wi-Fi connection to the Jetson CPU service timed out'),
  'wifi_connect_failed': ('wifi', 'Could not connect to the Jetson CPU Wi-Fi service'),
  'authentication_failed': ('authentication', 'CPU/Wi-Fi authentication or record integrity check failed'),
  'authentication_timeout': ('authentication', 'CPU/Wi-Fi authentication timed out; cause is not confirmed'),
  'authentication_unavailable': ('authentication', 'CPU/Wi-Fi peer closed during authentication'),
  'response_timeout': ('response', 'Jetson response timed out; network versus compute delay is unknown'),
  'remote_closed': ('response', 'Jetson closed the connection; network versus compute failure is unknown'),
  'inference_failed': ('inference', 'Jetson CPU ONNX inference failed'),
  'inference_invalid_result': ('inference', 'Jetson CPU returned an invalid ONNX result'),
  'protocol_invalid': ('protocol', 'CPU/Wi-Fi request or response is incompatible'),
}
NON_FAILURES = {'ok', 'awaiting_result', 'disabled', 'negotiating', 'session_ready'}


class RemoteFailure(ValueError):
  def __init__(self, code):
    self.code = code if isinstance(code, str) and code in ERRORS else 'protocol_invalid'
    self.stage, message = ERRORS[self.code]
    super().__init__(message)


def failure_response(meta, code, origin):
  error = RemoteFailure(code)
  if error.code in NON_FAILURES:
    error = RemoteFailure('protocol_invalid')
  return json.dumps({'id': meta['id'], 'sha256': meta['sha256'], 'processor': 'jetson_cpu_wifi',
                     'valid': False, 'errorCode': error.code, 'origin': origin}, separators=(',', ':')).encode()


def write_session(code, peer=None):
  peer = peer or {}
  value = {'code': RemoteFailure(code).code, 'updated': time.monotonic(), 'pid': os.getpid(),
           'diagnosticsSupported': peer.get(DIAGNOSTICS_CAPABILITY) is True}
  source = peer.get('carrot_source_commit')
  if isinstance(source, str) and re.fullmatch('[0-9a-f]{40}', source):
    value['sourceCommit'] = source
  temporary = None
  try:
    with tempfile.NamedTemporaryFile(mode='w', dir=SESSION_STATUS.parent, prefix=SESSION_STATUS.name + '.', delete=False) as stream:
      temporary = Path(stream.name)
      json.dump(value, stream)
    temporary.chmod(0o644)
    os.replace(temporary, SESSION_STATUS)
  except OSError:
    pass  # Optional display diagnostics cannot stop the driving link.
  finally:
    if temporary is not None:
      try:
        temporary.unlink(missing_ok=True)
      except OSError:
        pass


def session_diagnostic():
  try:
    with SESSION_STATUS.open() as stream:
      value = json.loads(stream.read(4097))
    if (not isinstance(value, dict) or value.get('code') not in ERRORS or not 0 <= time.monotonic() - value['updated']
        or type(value.get('pid')) is not int or not 0 < value['pid'] <= 2**31 - 1):
      return {}
    try:
      os.kill(int(value['pid']), 0)
    except ProcessLookupError:
      return {'code': 'disconnected'}
    except PermissionError:
      pass  # The daemon can run under a different account.
    result = {'code': value['code'], 'diagnosticsSupported': value.get('diagnosticsSupported') is True}
    source = value.get('sourceCommit')
    if isinstance(source, str) and re.fullmatch('[0-9a-f]{40}', source):
      result['sourceCommit'] = source
    return result
  except (OSError, ValueError, KeyError, TypeError):
    return {}


def read_exact(sock, size, deadline):
  result = bytearray()
  while len(result) < size:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
      raise TimeoutError('auxiliary vision deadline')
    sock.settimeout(remaining)
    part = sock.recv(size - len(result))
    if not part:
      raise ConnectionError('auxiliary vision closed')
    result.extend(part)
  return bytes(result)


def send_record(sock, data, deadline):
  sock.settimeout(max(.001, deadline - time.monotonic()))
  sock.sendall(PREFIX.pack(len(data)) + data)


def read_record(sock, limit, deadline):
  size, = PREFIX.unpack(read_exact(sock, PREFIX.size, deadline))
  if not 0 < size <= limit:
    raise ValueError('auxiliary vision size')
  return read_exact(sock, size, deadline)


def decode_request(data):
  if not PREFIX.size <= len(data) <= MAX_REQUEST:
    raise ValueError('auxiliary metadata truncated')
  size, = PREFIX.unpack(data[:PREFIX.size])
  if not 0 < size <= 1024 or len(data) < PREFIX.size + size:
    raise ValueError('auxiliary metadata size')
  value = json.loads(data[PREFIX.size:PREFIX.size + size])
  if not isinstance(value, dict) or set(value) != {'version', 'id', 'kind', 'sha256', 'threshold', 'op'}:
    raise ValueError('auxiliary metadata fields')
  kind = value['kind']
  if not isinstance(kind, str) or kind not in SHAPES or value['op'] not in ('infer', 'unload'):
    raise ValueError('auxiliary request kind/operation')
  expected = 1
  for dimension in SHAPES[kind]:
    expected *= dimension
  image = data[PREFIX.size + size:]
  if len(image) != (expected if value['op'] == 'infer' else 0) or type(value.get('version')) is not int or value['version'] != 1:
    raise ValueError('auxiliary image shape/version')
  if not isinstance(value.get('id'), str) or re.fullmatch('[0-9a-f]{32}', value['id']) is None:
    raise ValueError('auxiliary request identity')
  if not isinstance(value.get('sha256'), str) or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None:
    raise ValueError('auxiliary model identity')
  threshold = value.get('threshold')
  if isinstance(threshold, bool) or not isinstance(threshold, (float, int)) or not 0 <= threshold <= 1:
    raise ValueError('auxiliary threshold')
  return value, image


class CpuClient:
  def __init__(self, kind, model_path):
    self.kind, self.model_path = kind, model_path
    self.valid, self.error, self.sha256 = False, '', ''
    self.last_success = 0.
    self.error_code = 'awaiting_result'
    self.attempts = self.successes = self.failures = 0
    self.network_path = 'unknown'
    self.network_updated = 0.

  def load(self):
    try:
      self.sha256 = hashlib.sha256(Path(self.model_path).read_bytes()).hexdigest()
      self.valid, self.error = True, ''
    except OSError:
      self.valid = False
      self.fail('local_model_missing')
    return self.valid

  def request(self, pixels, threshold, op='infer', timeout=DEADLINE):
    started = time.monotonic()
    deadline = started + timeout
    request_id = secrets.token_hex(16)
    meta = json.dumps({'version': 1, 'id': request_id, 'kind': self.kind,
                       'sha256': self.sha256, 'threshold': threshold, 'op': op}, separators=(',', ':')).encode()
    data = PREFIX.pack(len(meta)) + meta + pixels
    try:
      with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(SOCKET)
        send_record(sock, data, deadline)
        value = json.loads(read_record(sock, MAX_RESPONSE, deadline))
    except (FileNotFoundError, ConnectionRefusedError):
      status = session_diagnostic()
      code = status.get('code', 'relay_unavailable')
      raise RemoteFailure(code if code not in ('session_ready', 'negotiating', 'ok') else 'relay_unavailable') from None
    except TimeoutError:
      raise RemoteFailure('response_timeout') from None
    except OSError:
      raise RemoteFailure('remote_closed') from None
    if (time.monotonic() > deadline or not isinstance(value, dict)
        or value.get('id') != request_id or value.get('sha256') != self.sha256
        or value.get('processor') != 'jetson_cpu_wifi'):
      raise RemoteFailure('protocol_invalid')
    self.network_path = value.get('wifiPath') if value.get('wifiPath') in ('direct_ap', 'shared_wifi') else 'unknown'
    self.network_updated = time.monotonic()
    if value.get('valid') is not True:
      error = RemoteFailure(value.get('errorCode', 'inference_invalid_result'))
      raise RemoteFailure('protocol_invalid') if error.code in NON_FAILURES else error
    if op == 'infer':
      if self.kind == 'lane':
        for side in ('left', 'right'):
          lane, score = value.get(side + 'Line'), value.get(side + 'Conf')
          if type(lane) is not int or lane not in (-1, 0, 1) or type(score) not in (int, float) or not 0 <= score <= 1:
            raise RemoteFailure('inference_invalid_result')
      else:
        score = value.get('confidence')
        if type(score) not in (int, float) or not 0 <= score <= 1:
          raise RemoteFailure('inference_invalid_result')
    return value

  def fail(self, code):
    self.last_success = 0.
    error = RemoteFailure(code)
    self.error_code, self.error = error.code, str(error)

  def diagnostic(self):
    session = session_diagnostic()
    code = self.error_code
    if self.last_success > 0 and self.remote_ready():
      code = 'ok'
    elif code == 'awaiting_result' or code == 'ok':
      code = session.get('code', 'awaiting_result')
    stage, message = ERRORS[code]
    return {**session, 'code': code, 'stage': stage, 'message': message,
            'wifiPath': self.network_path if 0 <= time.monotonic() - self.network_updated < 3 else 'unknown',
            'attempts': self.attempts, 'successes': self.successes,
            'failures': self.failures, 'inFlight': self.attempts - self.successes - self.failures}

  def infer(self, image, threshold=.25):
    import numpy as np
    if image.shape != SHAPES[self.kind] or image.dtype != np.uint8:
      raise ValueError('auxiliary input contract')
    self.attempts += 1
    try:
      value = self.request(np.ascontiguousarray(image).tobytes(), threshold)
    except RemoteFailure as error:
      self.failures += 1
      self.fail(error.code)
      raise
    except (OSError, ValueError, KeyError, TypeError):
      self.failures += 1
      self.fail('protocol_invalid')
      raise RemoteFailure(self.error_code) from None
    self.last_success, self.error, self.error_code = time.monotonic(), '', 'ok'
    self.successes += 1
    return value

  def remote_ready(self):
    return self.last_success > 0 and 0 <= time.monotonic() - self.last_success < MODEL_IDLE_SECONDS

  def unload(self):
    self.last_success = 0.
    if self.valid:
      try:
        self.request(b'', .25, op='unload', timeout=.1)
      except (OSError, ValueError, KeyError, TypeError):
        pass  # The host also expires unused models independently.
    self.valid = False


class RemoteLaneInference(CpuClient):
  def __init__(self, model_path):
    super().__init__('lane', model_path)

  def infer(self, image, width, height, conf_thresh=.25, **_):
    from .lane_inference import prepare_lane_image
    try:
      value = super().infer(prepare_lane_image(image, width, height), conf_thresh)
      for side in ('left', 'right'):
        lane, score = value[side + 'Line'], value[side + 'Conf']
        if type(lane) is not int or lane not in (-1, 0, 1) or type(score) not in (int, float) or not 0 <= score <= 1:
          raise ValueError('auxiliary lane output contract')
        if score < conf_thresh:
          value[side + 'Line'] = -1
      return value
    except (OSError, ValueError, KeyError, TypeError):
      self.last_success = 0.
      if self.error_code == 'ok':
        self.fail('inference_invalid_result')
      return {'leftLine': -1, 'rightLine': -1, 'leftConf': 0., 'rightConf': 0., 'candidatesCount': 0,
              'valid': False, 'error': self.error, 'errorCode': self.error_code}


def remote_vasm_type():
  from .v_asm_inference import VASMInference

  class RemoteVASM(VASMInference):
    def load(self):
      if self.valid and self.net is not None:
        return True
      self.client = CpuClient('vasm', self.model_path)
      self.valid = self.client.load()
      self.error = self.client.error
      self.net = True if self.valid else None
      return self.valid

    def unload(self):
      client = getattr(self, 'client', None)
      if client is not None:
        client.unload()
      self.client = None
      super().unload()

    def remote_ready(self):
      client = getattr(self, 'client', None)
      return client is not None and client.remote_ready()

    def diagnostic(self):
      client = getattr(self, 'client', None)
      return client.diagnostic() if client is not None else {'code': 'disabled', 'stage': 'disabled',
                                                            'message': ERRORS['disabled'][1]}

    def predict_square(self, square):
      try:
        result = self.client.infer(square)
        confidence = result.get('confidence')
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
          raise ValueError('auxiliary vehicle confidence contract')
      except (OSError, ValueError, KeyError, TypeError):
        if self.client.error_code == 'ok':
          self.client.fail('inference_invalid_result')
        self.error = self.client.error
        raise RemoteFailure(self.client.error_code) from None
      self.error = ''
      return float(confidence)

  return RemoteVASM


def remote_vasm(model_path):
  return remote_vasm_type()(model_path)
