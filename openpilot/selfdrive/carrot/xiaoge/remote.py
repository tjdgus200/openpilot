"""Bounded local RPC to the session-authenticated WLAN worker; never run ONNX here."""
import hashlib
import json
from pathlib import Path
import secrets
import socket
import struct
import time
import re

SOCKET = '/dev/shm/carrot-aux-vision.sock'
PORT = 7717
CAPABILITY = 'carrot_aux_cpu_wifi_v1'
DEADLINE = .75
MAX_REQUEST = 380_000
MAX_RESPONSE = 4096
PREFIX = struct.Struct('!I')
SHAPES = {'lane': (416, 416), 'vasm': (352, 352, 3)}
FILES = {'lane': 'lane.onnx', 'vasm': 'v_asm_model.onnx'}
MODEL_IDLE_SECONDS = 3.0


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

  def load(self):
    try:
      self.sha256 = hashlib.sha256(Path(self.model_path).read_bytes()).hexdigest()
      self.valid, self.error = True, ''
    except OSError:
      self.valid, self.error = False, 'auxiliary model identity unavailable'
    return self.valid

  def request(self, pixels, threshold, op='infer', timeout=DEADLINE):
    started = time.monotonic()
    deadline = started + timeout
    request_id = secrets.token_hex(16)
    meta = json.dumps({'version': 1, 'id': request_id, 'kind': self.kind,
                       'sha256': self.sha256, 'threshold': threshold, 'op': op}, separators=(',', ':')).encode()
    data = PREFIX.pack(len(meta)) + meta + pixels
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
      sock.settimeout(timeout)
      sock.connect(SOCKET)
      send_record(sock, data, deadline)
      value = json.loads(read_record(sock, MAX_RESPONSE, deadline))
    if (time.monotonic() > deadline or not isinstance(value, dict)
        or value.get('id') != request_id or value.get('sha256') != self.sha256
        or value.get('processor') != 'jetson_cpu_wifi' or value.get('valid') is not True):
      raise ValueError('auxiliary result invalid, late, or incompatible')
    return value

  def infer(self, image, threshold=.25):
    import numpy as np
    if image.shape != SHAPES[self.kind] or image.dtype != np.uint8:
      raise ValueError('auxiliary input contract')
    try:
      value = self.request(np.ascontiguousarray(image).tobytes(), threshold)
    except (OSError, ValueError, KeyError, TypeError):
      self.last_success = 0.
      self.error = 'Jetson CPU/Wi-Fi unavailable'
      raise
    self.last_success, self.error = time.monotonic(), ''
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
      return {'leftLine': -1, 'rightLine': -1, 'leftConf': 0., 'rightConf': 0., 'candidatesCount': 0,
                  'valid': False, 'error': 'Jetson CPU/Wi-Fi lane result unavailable'}


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

    def predict_square(self, square):
      try:
        result = self.client.infer(square)
        confidence = result.get('confidence')
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
          raise ValueError('auxiliary vehicle confidence contract')
      except (OSError, ValueError, KeyError, TypeError):
        self.client.last_success = 0.
        self.error = 'Jetson CPU/Wi-Fi BSD result unavailable'
        raise ValueError(self.error) from None
      self.error = ''
      return float(confidence)

  return RemoteVASM


def remote_vasm(model_path):
  return remote_vasm_type()(model_path)
