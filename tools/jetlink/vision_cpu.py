"""Auxiliary CPU vision only. Independent process, WLAN-only, one request in flight."""
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import socket
import select
import subprocess
import sys
import threading
import time


def process_limits(role):
  # Popen can inherit jetlinkd's FIFO policy. Drop it before importing any
  # helper/crypto/numerical library or creating threads in the new process.
  os.environ['OPENBLAS_NUM_THREADS'] = os.environ['OMP_NUM_THREADS'] = '1'
  os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
  os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  os.setpriority(os.PRIO_PROCESS, 0, 19 if role == 'send' else 10)
  os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])


if __name__ == '__main__':
  process_limits(sys.argv[1] if len(sys.argv) > 1 else 'receive')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from openpilot.selfdrive.carrot.xiaoge import remote as R
from hud_wifi_worker import authenticate, seal, unseal, wlan_addresses, target_addresses
from hud_wifi_route import select_link, wlan_address

IDLE_SECONDS = 5.


class Engine:
  def __init__(self):
    self.models = {}
    self.used = {}

  def expire(self):
    now = time.monotonic()
    for kind in list(self.models):
      if now - self.used[kind] >= R.MODEL_IDLE_SECONDS:
        del self.models[kind]
        del self.used[kind]

  def infer(self, data):
    # Imports and model allocation occur only in this separate low priority CPU process.
    import cv2
    import numpy as np
    from openpilot.selfdrive.carrot.xiaoge.lane_inference import LaneInference
    from openpilot.selfdrive.carrot.xiaoge.v_asm_inference import VASMInference
    value, image = R.decode_request(data)
    kind = value['kind']
    self.expire()
    if value['op'] == 'unload':
      self.models.pop(kind, None)
      self.used.pop(kind, None)
      return json.dumps({'id': value['id'], 'sha256': value['sha256'],
                         'processor': 'jetson_cpu_wifi', 'valid': True}).encode()
    if kind not in self.models:
      cv2.setNumThreads(1)
      cv2.ocl.setUseOpenCL(False)
      path = ROOT / 'openpilot/selfdrive/carrot/xiaoge/assets' / R.FILES[kind]
      model = (LaneInference if kind == 'lane' else VASMInference)(path)
      if not model.load():
        raise ValueError('auxiliary CPU model unavailable')
      # Explicit CPU targets for both models, never TensorRT/CUDA/OpenCL.
      model.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
      model.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
      self.models[kind] = model, hashlib.sha256(path.read_bytes()).hexdigest()
    self.used[kind] = time.monotonic()
    model, digest = self.models[kind]
    if value['sha256'] != digest:
      raise ValueError('auxiliary model identity differs')
    image = np.frombuffer(image, np.uint8).reshape(R.SHAPES[kind])
    started = time.monotonic()
    try:
      if kind == 'lane':
        result = model.infer(image, 416, 416, conf_thresh=value['threshold'])
      else:
        confidence = model.predict_square(image)
        result = {'valid': True, 'confidence': confidence}
    except cv2.error:
      raise ValueError('auxiliary CPU inference failed') from None
    return json.dumps({**result, 'id': value['id'], 'sha256': digest,
                       'processor': 'jetson_cpu_wifi', 'hostMs': (time.monotonic() - started) * 1000},
                      separators=(',', ':'), allow_nan=False).encode()


class Runtime:
  def __init__(self, role, key, control):
    self.role, self.key, self.control = role, hmac.digest(key, b'aux-cpu-wifi-v1', 'sha256'), control
    self.stop = threading.Event()
    self.busy = threading.Lock()
    self.engine = Engine() if role == 'receive' else None
    self.connection = None
    self.link = None
    self.sequence = 0
    self.next_cpu = 0.

  def close_link(self):
    if self.connection is not None:
      self.connection.close()
    self.connection, self.link = None, None

  def relay(self, data):
    # Recheck before every image: no USB/NCM/Ethernet or cached-route fallback.
    link = select_link(tuple(wlan_addresses()), target_addresses())
    if not link:
      raise ConnectionError('auxiliary WLAN unavailable')
    if self.link != link:
      self.close_link()
    if self.connection is None:
      _, source, target = link
      sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      try:
        sock.settimeout(R.DEADLINE)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.bind((source, 0))
        sock.connect((target, R.PORT))
        _, key = authenticate(sock, self.key, 1)
      except BaseException:
        sock.close()
        raise
      self.connection, self.link, self.record_key, self.sequence = sock, link, key, 0
    self.sequence += 1
    if self.sequence >= 2**32:
      raise ValueError('auxiliary session sequence exhausted')
    header = R.PREFIX.pack(self.sequence)
    deadline = time.monotonic() + R.DEADLINE
    R.send_record(self.connection, header + seal(self.record_key, self.sequence, 0, header, data), deadline)
    answer = R.read_record(self.connection, R.MAX_RESPONSE + 16, deadline)
    return unseal(self.record_key, self.sequence, 1, header, answer)

  def handle(self, sock):
    try:
      with sock:
        if not self.busy.acquire(blocking=False):
          return  # Never queue old camera frames behind another inference.
        try:
          if self.role == 'send':
            deadline = time.monotonic() + R.DEADLINE
            data = R.read_record(sock, R.MAX_REQUEST, deadline)
            R.decode_request(data)
            R.send_record(sock, self.relay(data), deadline)
          else:
            self.receive(sock)
        finally:
          self.busy.release()
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError, subprocess.SubprocessError):
      if self.role == 'send':
        self.close_link()

  def receive(self, sock):
    _, key = authenticate(sock, self.key)
    number = 0
    idle_deadline = time.monotonic() + IDLE_SECONDS
    while not self.stop.is_set():
      remaining = idle_deadline - time.monotonic()
      if remaining <= 0:
        raise TimeoutError('auxiliary idle connection expired')
      # A legitimate low-rate caller may be idle for >1s. Reauthentication is
      # not inference; retain the connection across ordinary request intervals.
      # Poll idle connections without holding models in memory indefinitely.
      if not select.select([sock, self.control] if self.control is not None else [sock], [], [], min(.2, remaining))[0]:
        self.engine.expire()
        continue
      if self.control is not None and select.select([self.control], [], [], 0)[0]:
        self.stop.set()
        return
      data = R.read_record(sock, R.MAX_REQUEST + 20, time.monotonic() + R.DEADLINE)
      if len(data) <= 20:
        raise ValueError('auxiliary encrypted record truncated')
      sequence, = R.PREFIX.unpack(data[:4])
      if sequence != number + 1:
        raise ValueError('auxiliary sequence')
      plain = unseal(key, sequence, 0, data[:4], data[4:])
      started = time.monotonic()
      # Bound sustained compute duty to 80% of one CPU thread. Delay rather
      # than create catch-up jobs; the comma capture deadline still applies.
      self.stop.wait(max(0., self.next_cpu - started))
      compute_started = time.monotonic()
      result = self.engine.infer(plain)
      self.next_cpu = time.monotonic() + (time.monotonic() - compute_started) * .25
      if time.monotonic() - started > R.DEADLINE:
        raise TimeoutError('auxiliary CPU deadline')
      R.send_record(sock, seal(key, sequence, 1, data[:4], result), started + R.DEADLINE)
      number = sequence
      idle_deadline = time.monotonic() + IDLE_SECONDS

  def run(self):
    self.control.send(b'ready')
    try:
      while not self.stop.is_set():
        listeners = []
        try:
          network = tuple(wlan_addresses()) if self.role == 'receive' else None
          if self.engine is not None:
            self.engine.expire()
          if self.role == 'receive' and not network:
            if select.select([self.control], [], [], 0)[0]:
              break
            self.stop.wait(.5)
            continue
          family = socket.AF_INET if self.role == 'receive' else socket.AF_UNIX
          binds = [R.SOCKET] if self.role == 'send' else [(address, R.PORT) for _, address in network]
          for address in binds:
            listener = socket.socket(family, socket.SOCK_STREAM)
            listeners.append(listener)
            if self.role == 'send':
              # Parent is the sole session owner and reaps the previous worker first.
              Path(R.SOCKET).unlink(missing_ok=True)
              listener.bind(address)
              os.chmod(R.SOCKET, 0o600)
            else:
              listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
              listener.bind(address)
            listener.listen(1)
            listener.setblocking(False)
          next_network = time.monotonic() + 1.
          while not self.stop.is_set():
            if self.role == 'receive' and time.monotonic() >= next_network:
              if tuple(wlan_addresses()) != network:
                break
              self.engine.expire()
              next_network = time.monotonic() + 1.
            ready, _, _ = select.select([self.control, *listeners], [], [], .2)
            if self.control in ready:
              self.stop.set()
              break
            for listener in ready:
              sock, peer = listener.accept()
              if self.role == 'receive' and not wlan_address(peer[0]):
                sock.close()
                continue
              # One worker; bounded backlog, no inference thread pool.
              self.handle(sock)
        except (OSError, ValueError, subprocess.SubprocessError):
          self.stop.wait(.5)
        finally:
          for listener in listeners:
            listener.close()
    finally:
      self.close_link()
      if self.role == 'send':
        Path(R.SOCKET).unlink(missing_ok=True)


def main():
  role = sys.argv[1]
  if role not in ('send', 'receive', '--check-models'):
    raise ValueError('auxiliary role')
  ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
  if os.getppid() == 1:
    return
  if role == '--check-models':
    check_models()
    return
  fd = int(sys.argv[2])
  with socket.socket(fileno=fd) as control:
    config = json.loads(control.recv(512))
    key = bytes.fromhex(config['key'])
    if len(key) != 32 or config['port'] != R.PORT:
      raise ValueError('auxiliary configuration')
    runtime = Runtime(role, key, control)
    signal.signal(signal.SIGTERM, lambda *_: runtime.stop.set())
    runtime.run()


def check_models():
  """Isolated installation probe, before allocating the driving GPU engine."""
  import numpy as np
  engine = Engine()
  for kind, shape in R.SHAPES.items():
    path = ROOT / 'openpilot/selfdrive/carrot/xiaoge/assets' / R.FILES[kind]
    metadata = json.dumps({'version': 1, 'id': '0' * 32, 'kind': kind, 'op': 'infer',
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'threshold': .25}).encode()
    result = json.loads(engine.infer(R.PREFIX.pack(len(metadata)) + metadata + np.zeros(shape, np.uint8).tobytes()))
    if result.get('valid') is not True:
      raise ValueError('Candidate auxiliary CPU model probe failed')
  print('AUX_CPU_PROBE_OK', flush=True)


if __name__ == '__main__':
  main()
