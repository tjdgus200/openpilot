"""Opt-in memory snapshots inside tombstoned. Read the original reporter's proof without changing it."""
from collections import deque
import hashlib
import http.client
import json
import secrets
import socket
import threading
import time

from openpilot.system.pv5_incidents.core import MAX_AGE, MAX_ROWS, Retry, encode, signature, stopped
from openpilot.system.pv5_incidents.memory_pressure import MemoryPressure, validate_memory
from openpilot.system.pv5_incidents.runtime import lower_priority


class Transfer(threading.Thread):
  def __init__(self, reporter, data):
    super().__init__(name='pv5-memory-upload', daemon=True)
    self.reporter, self.data = reporter, data
    self.generation = reporter.generation
    self.cancelled = threading.Event()
    self.connection = None
    self.acknowledged = False

  def allowed(self):
    reporter = self.reporter
    return (not self.cancelled.is_set() and not reporter.stop.is_set() and reporter.generation == self.generation
            and stopped(reporter.proof, time.monotonic()))

  def cancel(self):
    self.cancelled.set()
    sock = getattr(self.connection, 'sock', None)
    if sock is not None:
      try:
        sock.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass  # The upload thread closes its own connection.

  def run(self):
    lower_priority()
    try:
      if not self.allowed() or not self.reporter.enabled():
        return
      connection = self.reporter.factory()  # Separate socket, same private HTTPS endpoint and TLS configuration.
      self.connection = connection
      connection.connect()
      connection.auto_open = 0
      stamp, nonce = int(time.time()), secrets.token_hex(16)
      headers = {'Content-Type': 'application/json', 'X-PV5-Time': str(stamp), 'X-PV5-Nonce': nonce,
                 'X-PV5-Signature': signature(self.reporter.key, stamp, nonce, self.data)}
      if not self.reporter.enabled() or not self.allowed():
        return
      connection.request('POST', '/v1/incident', body=self.data, headers=headers)
      response = connection.getresponse()
      result = response.read(2049)
      self.acknowledged = (response.status == 200 and len(result) <= 2048 and
                           json.loads(result) == {'accepted': hashlib.sha256(self.data).hexdigest()})
    except (OSError, ValueError, http.client.HTTPException):
      pass  # Retry the same immutable envelope and ID after backoff.
    finally:
      if self.connection is not None:
        self.connection.close()


class MemorySender:
  def __init__(self, reporter):
    self.reporter = reporter
    self.capture, self.retry = MemoryPressure(), Retry()
    self.queue = deque(maxlen=MAX_ROWS)
    self.transfer = None

  def collect(self, device, processes, now):
    proof = self.reporter.proof
    if proof is None or not proof.pv5 or not self.capture.due(device, now):
      return
    processes.update(0)  # Read the latest conflated process list only when pressure and the interval qualify.
    fact = self.capture.observe(processes, time.monotonic(), self.reporter.source, device)
    if fact is not None:
      stamp = int(time.time())
      data = encode({'format': 2, 'id': secrets.token_hex(16), 'captured_at': stamp, 'fact': fact})
      validate_memory(data)
      self.queue.append((stamp, data))

  def pump(self, now):
    transfer = self.transfer
    if transfer is not None:
      if transfer.is_alive():
        if not transfer.allowed():
          transfer.cancel()
        return
      self.transfer = None
      if transfer.acknowledged:
        if self.queue and self.queue[0][1] == transfer.data:
          self.queue.popleft()
        self.retry.succeeded(now)
      elif not transfer.cancelled.is_set():
        self.retry.failed(now)

    wall = time.time()
    while self.queue and not -300 <= wall - self.queue[0][0] <= MAX_AGE:
      self.queue.popleft()
    if (self.queue and not self.reporter.stop.is_set() and stopped(self.reporter.proof, now)
        and self.retry.ready(now)):
      self.transfer = Transfer(self.reporter, self.queue[0][1])
      self.transfer.start()

  def run(self):
    lower_priority()
    try:
      import openpilot.cereal.messaging as messaging
      device = messaging.SubMaster(['deviceState'])
      processes = messaging.SubMaster(['procLog', 'managerState'])
      next_sample = 0.
      while not self.reporter.stop.is_set():
        self.pump(time.monotonic())
        now = time.monotonic()
        if now >= next_sample:
          next_sample = now + .5
          device.update(0)
          self.collect(device, processes, time.monotonic())
        self.reporter.stop.wait(.1 if self.transfer is not None else .5)
    except Exception:
      pass  # An unavailable diagnostic must not affect the registered incident reporter.
    finally:
      if self.transfer is not None:
        self.transfer.cancel()


def start_memory_reporting(reporter):
  sender = MemorySender(reporter)
  threading.Thread(target=sender.run, name='pv5-memory-observer', daemon=True).start()
