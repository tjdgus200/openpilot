"""Jetson-local, low-frequency footer metrics; never read on the render path."""
import json
import math
import os
from pathlib import Path
import socket
import threading
import time

from cluster_system_monitor import SystemStatsSampler as LocalSystemStatsSampler

GPU_PATHS = (Path('/sys/class/devfreq/17000000.gpu/device/load'),
             Path('/sys/devices/platform/bus@0/17000000.gpu/load'),
             Path('/sys/devices/platform/17000000.gpu/load'))
CONTROL = '/run/carrot-jetlink/control.sock'


def gpu_percent(paths=GPU_PATHS):
  for path in paths:
    try:
      value = float(path.read_text().strip())
      if math.isfinite(value) and 0 <= value <= 1000:
        return value / 10.0
    except (OSError, TypeError, ValueError, UnicodeError):
      pass
  return None


def valid_timing(value):
  if not isinstance(value, dict) or value.get('window_s') != 10.0:
    return None
  age, count = value.get('last_frame_age_s'), value.get('count')
  if (isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age < 2
      or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 2000):
    return None
  for group in ('infer_ms', 'server_ms'):
    fields = value.get(group)
    if not isinstance(fields, dict):
      return None
    for key in ('mean', 'p99', 'max'):
      number = fields.get(key)
      if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 10000:
        return None
    if fields['max'] < max(fields['mean'], fields['p99']):
      return None
  return value


class RuntimeFooterSampler:
  def __init__(self):
    self.local = LocalSystemStatsSampler(refresh_interval_s=1.)
    self.lock = threading.Lock()
    self.stop = threading.Event()
    self.value = {}
    self.sock = None
    self.pending = bytearray()
    self.sequence = 0
    self.retry_at = 0.
    self.thread = threading.Thread(target=self._run, name='hud-runtime-stats', daemon=True)
    self.thread.start()

  def sample(self):
    with self.lock:
      value = dict(self.value)
    return value if 0 <= time.monotonic() - value.get('updated', -1e10) < 3 else {}

  def _disconnect(self):
    if self.sock is not None:
      self.sock.close()
    self.sock = None
    self.pending.clear()

  def _timing(self):
    now = time.monotonic()
    if self.sock is None:
      if now < self.retry_at:
        return None
      self.retry_at = now + 5.
      self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
      self.sock.settimeout(.15)
      self.sock.connect(CONTROL)
    self.sequence += 1
    self.sock.sendall(json.dumps({'id': self.sequence, 'cmd': 'display_stats'}).encode() + b'\n')
    deadline = time.monotonic() + .2
    for _ in range(32):
      if time.monotonic() >= deadline:
        break
      while b'\n' not in self.pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise TimeoutError('Display statistics deadline')
        self.sock.settimeout(min(.15, remaining))
        data = self.sock.recv(8192)
        if not data:
          raise ConnectionError('Control channel closed')
        self.pending.extend(data)
        if len(self.pending) > 65536:
          raise ValueError('Display control budget exceeded')
      line, _, remainder = self.pending.partition(b'\n')
      self.pending = bytearray(remainder)
      event = json.loads(line)
      if event.get('event') == 'reply' and event.get('id') == self.sequence:
        return valid_timing(event.get('display_stats')) if event.get('ok') is True else None
    raise TimeoutError('Display statistics deadline')

  def _run(self):
    try:
      if hasattr(os, 'sched_setscheduler'):
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
    except (OSError, PermissionError):
      pass
    while not self.stop.is_set():
      started = time.monotonic()
      stats = self.local.sample()
      timing = None
      try:
        timing = self._timing()
      except (OSError, ValueError, TypeError, AttributeError):
        self._disconnect()
      value = {'updated': time.monotonic(), 'cpu_percent': stats.cpu_used_percent,
               'memory_percent': stats.memory_used_percent, 'gpu_percent': gpu_percent(), 'timing': timing}
      with self.lock:
        self.value = value
      self.stop.wait(max(0, started + 1. - time.monotonic()))
    self._disconnect()

  def close(self):
    self.stop.set()
    self.thread.join(timeout=1.)
    self.local.close()


def footer_lines(vehicle, host, language='ko'):
  def percentage(value):
    return '--%' if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100 else f'{value:.0f}%'
  first = (f"comma CPU {percentage(vehicle.cpu_used_percent)} MEM {percentage(vehicle.memory_used_percent)}"
           f"  |  Jetson CPU {percentage(host.get('cpu_percent'))} MEM {percentage(host.get('memory_percent'))} GPU {percentage(host.get('gpu_percent'))}")
  timing = valid_timing(host.get('timing'))
  if timing:
    values = timing['infer_ms']
    second = (f"Jetson infer 10s  AVG {values['mean']:.1f}  P99 {values['p99']:.1f}  MAX {values['max']:.1f} ms"
              f"  (n={timing['count']})")
  else:
    second = 'Jetson infer 10s  AVG --  P99 --  MAX -- ms'
  return first, second
