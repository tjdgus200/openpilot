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
QUALITY_STATUS = {}
QUALITY_UPDATED = 0.


def quality_status():
  return QUALITY_STATUS if 0 <= time.monotonic() - QUALITY_UPDATED < 3 else {}


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
    from hud_quality import RUN_PROFILE, RUN_FPS
    from hud_quality_compare import Comparison
    self.comparison = Comparison(*RUN_PROFILE, kind='fps-q92' if RUN_FPS else 'codec') if RUN_PROFILE is not None else None
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
    request = {'id': self.sequence, 'cmd': 'display_stats'}
    if getattr(self, 'comparison', None) is not None:
      request['include_samples'] = True
    self.sock.sendall(json.dumps(request).encode() + b'\n')
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
    global QUALITY_STATUS, QUALITY_UPDATED
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
      if self.comparison is not None:
        QUALITY_STATUS = self.comparison.update(timing)
        QUALITY_UPDATED = time.monotonic()
        value['quality'] = QUALITY_STATUS
      with self.lock:
        self.value = value
      self.stop.wait(max(0, started + 1. - time.monotonic()))
    self._disconnect()

  def close(self):
    self.stop.set()
    self.thread.join(timeout=1.)
    self.local.close()


TEXT = (215, 220, 225)
NORMAL = (255, 255, 255)
CAUTION = (255, 165, 60)
HIGH = (255, 80, 80)
UNKNOWN = (155, 160, 165)


def metric_color(value, kind):
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
    return UNKNOWN
  # Display-only headroom cues, never fault classification or control input.
  # High GPU utilization alone is not an error or a reason to reduce inference.
  warn, high = {'cpu': (80, 95), 'memory': (85, 95), 'infer': (35, 50)}.get(kind, (math.inf, math.inf))
  return HIGH if value >= high else CAUTION if value >= warn else NORMAL


def footer_segments(vehicle, host, language='ko'):
  result = []
  def usage(label, value, kind):
    valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100
    result.extend(((label, TEXT), (f'{value:.0f}%' if valid else '--%', metric_color(value if valid else None, kind))))
  usage('COMMA CPU:', vehicle.cpu_used_percent, 'cpu')
  usage(' MEM:', vehicle.memory_used_percent, 'memory')
  usage(' | ORIN CPU:', host.get('cpu_percent'), 'cpu')
  usage(' MEM:', host.get('memory_percent'), 'memory')
  usage(' GPU:', host.get('gpu_percent'), 'gpu')
  return tuple(result)


def inference_segments(host):
  from hud_quality import SHORT_NAMES
  quality = host.get('quality') or {}
  if quality.get('kind') == 'fps-q92':
    from hud_rate import SHORT_NAMES
  active = quality.get('effective')
  result = [(f'{SHORT_NAMES[active]} 10s ' if type(active) is int and 0 <= active < 8 else 'Inf10s ', TEXT)]
  timing = valid_timing(host.get('timing'))
  for label, key in (('AVG:', 'mean'), (' P99:', 'p99'), (' MAX:', 'max')):
    value = timing['infer_ms'][key] if timing else None
    result.extend(((label, TEXT), ('--' if value is None else f'{value:.1f}', metric_color(value, 'infer'))))
  result.append(('ms', TEXT))
  best = quality.get('best')
  if type(best) is int and 0 <= best < 8:
    result.append((f' B:{SHORT_NAMES[best]}', NORMAL))
  return tuple(result)


def footer_lines(vehicle, host, language='ko'):
  return (''.join(text for text, _ in footer_segments(vehicle, host, language)),
          ''.join(text for text, _ in inference_segments(host)))
