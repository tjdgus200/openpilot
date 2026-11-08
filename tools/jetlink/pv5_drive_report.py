"""PC-only, read-only PV5 route diagnostics. Never imported by driving processes.

Analyze full-schema rlogs, without extra inference, CAN requests or live profiling.
Reports contain bounded numeric traces and aggregates, never raw events or images.
"""
import argparse
from collections import Counter, OrderedDict, deque
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys


SERVICES = frozenset(('carParams', 'carState', 'carControl', 'carOutput', 'controlsState',
                      'modelV2', 'roadCameraState', 'wideRoadCameraState', 'logMessage'))
PHASES = frozenset(('camera_age_at_run_ms', 'inference_ms', 'postprocess_ms', 'usb_send_ms',
                    'usb_response_ms', 'usb_exchange_ms', 'ipc_reply_ms', 'server_gpu_ms',
                    'server_queue_ms', 'server_total_ms', 'runqueue_wait_ms', 'display_tail_ms'))


def finite(value):
  return type(value) in (int, float) and math.isfinite(value)


class Metric:
  """Whole-route mean/RMS/max, exact percentiles of the last 4096 valid samples."""
  def __init__(self):
    self.n, self.total, self.squares, self.maximum = 0, 0., 0., -math.inf
    self.values = deque(maxlen=4096)

  def add(self, value):
    if finite(value):
      self.n += 1
      self.total += value
      self.squares += value * value
      self.maximum = max(self.maximum, value)
      self.values.append(value)

  def result(self):
    if not self.n:
      return None
    ordered = sorted(self.values)
    return {'n': self.n, 'mean': self.total / self.n, 'rms': math.sqrt(self.squares / self.n), 'max': self.maximum,
            'p99_recent': ordered[math.ceil(.99 * len(ordered)) - 1], 'percentile_samples': len(ordered)}


def y_at(line, distance):
  xs, ys = line.get('x', ()), line.get('y', ())
  if not 2 <= len(xs) == len(ys) <= 33 or not finite(distance):
    return None
  if not all(finite(x) and finite(y) for x, y in zip(xs, ys, strict=True)) or any(b <= a for a, b in zip(xs, xs[1:], strict=False)):
    return None
  for i in range(1, len(xs)):
    if xs[i - 1] <= distance <= xs[i]:
      f = (distance - xs[i - 1]) / (xs[i] - xs[i - 1])
      return ys[i - 1] + f * (ys[i] - ys[i - 1])
  return None


class DriveReport:
  def __init__(self):
    self.latest, self.cameras, self.metrics = {}, {'roadCameraState': OrderedDict(), 'wideRoadCameraState': OrderedDict()}, {}
    self.counts, self.center_seconds, self.coverage = Counter(), Counter(), Counter()
    self.trace = deque(maxlen=1200)
    self.first, self.last, self.control_at, self.center = None, None, None, None
    self.next_trace, self.pv5 = 0., False
    self.timing = {}

  def metric(self, key, value):
    self.metrics.setdefault(key, Metric()).add(value)

  def fresh(self, name, now, limit=.2):
    item = self.latest.get(name)
    return item[1] if item is not None and item[2] and 0 <= now - item[0] <= limit else None

  def feed(self, name, now, data, valid=True):
    if name not in SERVICES or not finite(now) or now <= 0 or not isinstance(data, dict):
      return
    self.counts[name] += 1
    previous = self.latest.get(name)
    if previous is not None and now <= previous[0]:
      self.coverage['duplicate_or_out_of_order'] += 1
      return
    self.first = now if self.first is None else min(self.first, now)
    self.last = now if self.last is None else max(self.last, now)
    self.latest[name] = (now, data, valid)
    if name == 'carParams':
      self.pv5 = valid and data.get('carFingerprint') == 'KIA_PV5' and not data.get('notCar', False)
    if not valid:
      if name == 'controlsState':
        self.control_at, self.center = None, None
      return
    if name in self.cameras:
      frames = self.cameras[name]
      frames[data.get('frameId')] = (now, data)
      if len(frames) > 64:
        frames.popitem(last=False)
      for field in ('gain', 'measuredGreyFraction', 'targetGreyFraction', 'exposureValPercent'):
        self.metric(name + '.' + field, data.get(field))
      grey, target = data.get('measuredGreyFraction'), data.get('targetGreyFraction')
      if finite(grey) and finite(target) and 0 <= grey <= 1 and 0 < target <= 1:
        self.metric(name + '.grey_to_target', grey / target)
    elif name == 'modelV2':
      self._model(now, data)
    elif name == 'logMessage':
      self._timing(data)
    elif name == 'controlsState' and self.pv5:
      self._control(now, data)

  def _model(self, now, data):
    call = data.get('modelExecutionTime')
    if finite(call) and 0 < call < 10:
      self.metric('model_call_ms', call * 1000)
    eof = data.get('timestampEof')
    if finite(eof) and 0 < now - eof * 1e-9 < 10:
      self.metric('main_eof_to_model_ms', (now - eof * 1e-9) * 1000)
    for name, field in (('roadCameraState', 'frameId'), ('wideRoadCameraState', 'frameIdExtra')):
      matched = self.cameras[name].get(data.get(field))
      if matched is not None and 0 <= now - matched[0] < 1:
        self.coverage[name + '.matched'] += 1
        sof, camera_eof = (matched[1].get(k) for k in ('timestampSof', 'timestampEof'))
        if finite(sof) and finite(camera_eof) and 0 < sof <= camera_eof <= now * 1e9:
          self.metric(name + '.sof_to_model_ms', (now - sof * 1e-9) * 1000)
      else:
        self.coverage[name + '.unmatched'] += 1
    probs = data.get('laneLineProbs', ())
    if len(probs) == 4 and all(finite(p) and 0 <= p <= 1 for p in probs):
      self.metric('lane_min_probability', min(probs[1:3]))

  def _control(self, now, data):
    status = data.get('pv5LaneCenterStatus', 'off')
    reason = data.get('pv5LaneCenterWaitReason', 'none') if status == 'waiting' else status
    if self.control_at is not None and self.center is not None:
      duration = min(.2, now - self.control_at)
      self.center_seconds[self.center] += duration
      self.coverage['control_gap_seconds'] += max(0., now - self.control_at - duration)
    self.control_at, self.center = now, reason
    cs, cc = self.fresh('carState', now, .1), self.fresh('carControl', now)
    output, model = self.fresh('carOutput', now), self.fresh('modelV2', now)
    if (not cs or not cc or not output or not model or not cc.get('latActive') or not cs.get('canValid')
        or cs.get('canTimeout') or cs.get('steeringPressed') or cs.get('steerFaultTemporary')
        or cs.get('steerFaultPermanent') or cs.get('gearShifter') != 'drive'):
      self.coverage['tracking_unavailable_or_inactive'] += 1
      return
    requested = cc.get('actuators', {}).get('steeringAngleDeg')
    applied = output.get('actuatorsOutput', {}).get('steeringAngleDeg')
    measured, speed = cs.get('steeringAngleDeg'), cs.get('vEgo')
    if not all(finite(v) for v in (requested, applied, measured, speed)):
      return
    self.metric('request_minus_applied_deg', requested - applied)
    self.metric('applied_minus_measured_deg', applied - measured)
    self.metric('abs_tracking_deg', abs(applied - measured))
    lanes = model.get('laneLines', ())
    offset = None
    if len(lanes) == 4 and speed > 0:
      distance = max(5., speed * 1.5)
      left, right, path = (y_at(line, distance) for line in (lanes[1], lanes[2], model.get('position', {})))
      probs = model.get('laneLineProbs', ())
      if (all(finite(v) for v in (left, right, path)) and 2.8 <= right - left <= 4
          and len(probs) == 4 and all(finite(p) and .85 <= p <= 1 for p in probs[1:3])):
        offset = (left + right) / 2 - path
        self.metric('model_path_to_lane_center_m', offset)
    eof = model.get('timestampEof')
    model_at = self.latest['modelV2'][0]
    self.metric('model_to_controls_publish_ms', (now - model_at) * 1000)
    if finite(eof) and 0 < now - eof * 1e-9 < 10:
      self.metric('main_eof_to_controls_publish_ms', (now - eof * 1e-9) * 1000)
    if now >= self.next_trace:
      self.next_trace = now + .1
      self.trace.append({'time_s': now - self.first, 'requested_deg': requested, 'applied_deg': applied,
                         'measured_deg': measured, 'speed_ms': speed, 'path_offset_m': offset, 'center': reason})

  def _timing(self, data):
    try:
      value = json.loads(data.get('text', ''))
    except (TypeError, ValueError):
      return
    if not isinstance(value, dict) or value.get('msg') != 'runtimeTiming' or not isinstance(value.get('metrics'), dict):
      return
    component = value.get('component')
    if component not in ('modeld', 'jetlinkd'):
      return
    # Keep weighted means and reported maxima. Aggregate means do not yield a
    # per-frame P99; never turn them into one or subtract unrelated maxima.
    for key, sample in value['metrics'].items():
      if key not in PHASES or not isinstance(sample, dict):
        continue
      mean, maximum, count = (sample.get(k) for k in ('mean', 'max', 'count'))
      if finite(mean) and finite(maximum) and type(count) is int and 0 < count < 100000:
        entry = self.timing.setdefault(component + '.' + key, [0., 0, -math.inf])
        entry[0] += mean * count
        entry[1] += count
        entry[2] = max(entry[2], maximum)
    scheduler = value.get('scheduler')
    if isinstance(scheduler, dict):
      for key in ('runqueue_wait_ms', 'cpu_ms'):
        amount = scheduler.get(key)
        if finite(amount) and 0 <= amount < 100000:
          self.metric(component + '.scheduler_interval_' + key, amount)

  def result(self):
    duration = sum(self.center_seconds.values())
    return {
      'schema': 1, 'pv5_identified': self.pv5, 'observed_s': 0. if self.first is None else self.last - self.first,
      'service_counts': dict(self.counts), 'coverage': dict(self.coverage),
      'center': {'observed_s': duration, 'seconds': dict(self.center_seconds),
                 'correcting_fraction': self.center_seconds['correcting'] / duration if duration else None},
      'metrics': {key: metric.result() for key, metric in sorted(self.metrics.items())},
      'optional_runtime_phases': {key: {'mean': total / count, 'n': count, 'reported_max': maximum}
                                  for key, (total, count, maximum) in sorted(self.timing.items())},
      'tracking_trace_recent': list(self.trace),
      'limits': ['PC offline only; no additional vehicle collection or controller changes.',
                 'Trace: last 1200 samples at most 10Hz; P99: last 4096 values, not whole-route P99.',
                 'Tracking pairs are fresh latest samples, not command-response identity or measured actuator latency.',
                 'EOF to controls publication excludes CAN delivery and physical actuator response.',
                 'Phase breakdown unavailable unless the existing optional runtimeTiming was recorded.',
                 'Camera grey/gain and lane probabilities are not ground-truth illumination or detection accuracy.',
                 'No automatic PID, exposure, FOV, steering-limit or model changes.'],
    }


def read_events(path):
  """Stream full cereal messages with bounded allocation, including on Windows."""
  import bz2
  from contextlib import ExitStack
  from openpilot.cereal import log

  def exact(stream, size):
    blocks, count = [], 0
    while count < size:
      block = stream.read(size - count)
      if not block:
        raise ValueError('Truncated Capnp event')
      blocks.append(block)
      count += len(block)
    return b''.join(blocks)

  with ExitStack() as stack:
    stream = stack.enter_context(path.open('rb'))
    if path.suffix == '.zst':
      import zstandard
      stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(stream))
    elif path.suffix == '.bz2':
      stream = stack.enter_context(bz2.BZ2File(stream))
    while first := stream.read(4):
      if len(first) != 4:
        first += exact(stream, 4 - len(first))
      segments = struct.unpack('<I', first)[0] + 1
      if not 1 <= segments <= 64:
        raise ValueError('Invalid Capnp segment count')
      table = exact(stream, (segments + (segments % 2 == 0)) * 4)
      size = sum(struct.unpack('<' + 'I' * segments, table[:segments * 4])) * 8
      if size > 32 * 1024 * 1024:
        raise ValueError('Capnp event exceeds 32MiB bound')
      with log.Event.from_bytes(first + table + exact(stream, size)) as event:
        name = event.which()
        if name in SERVICES:
          data = {'text': str(event.logMessage)} if name == 'logMessage' else getattr(event, name).to_dict()
          yield name, event.logMonoTime * 1e-9, data, event.valid


def markdown(report):
  lines = ['# PV5 주행 진단', '', f"PV5 확인: {report['pv5_identified']} / 관측 {report['observed_s']:.1f}초", '',
           '## 중앙 보정 상태별 시간', '']
  lines += [f'- {key}: {value:.2f}초' for key, value in sorted(report['center']['seconds'].items(), key=lambda item: -item[1])]
  lines += ['', '## 지연·추종·노출', '', '| 지표 | N | 평균 | 최근 P99 | 최대 |', '|---|---:|---:|---:|---:|']
  for key, value in report['metrics'].items():
    if value:
      lines.append(f"| {key} | {value['n']} | {value['mean']:.4f} | {value['p99_recent']:.4f} | {value['max']:.4f} |")
  lines += ['', '## 선택적 기존 단계별 진단', '']
  if report['optional_runtime_phases']:
    lines += ['| 구간 | N | 가중 평균 ms | 기록된 최대 ms |', '|---|---:|---:|---:|']
    for key, value in report['optional_runtime_phases'].items():
      lines.append(f"| {key} | {value['n']} | {value['mean']:.3f} | {value['reported_max']:.3f} |")
  else:
    lines.append('미수집: runtimeTiming이 없는 로그에서 구간별 시간을 추정하지 않습니다.')
  if 'offline_tegrastats' in report:
    lines += ['', '## 별도 Jetson 자원 기록', '']
    for key, value in report['offline_tegrastats'].items():
      if value:
        lines.append(f"- {key}: 평균 {value['mean']:.2f}, 최대 {value['max']:.2f}")
  lines += ['', '## 해석 범위', '', *('- ' + value for value in report['limits'])]
  return '\n'.join(lines) + '\n'


def tegrastats_metrics(path):
  """Optional offline EMC/GPU statistics; no live tool is launched or stopped."""
  metrics = {'emc_percent_at_current_frequency': Metric(), 'emc_mhz': Metric(), 'gpu_active_percent': Metric()}
  with path.open(encoding='utf-8', errors='replace') as stream:
    while line := stream.readline(4097):
      if len(line) > 4096:
        raise ValueError('Tegrastats line exceeds 4096 characters')
      emc = re.search(r'\bEMC_FREQ\s+(\d+)%@(\d+)\b', line)
      gpu = re.search(r'\bGR3D_FREQ\s+(\d+)%', line)
      if emc and 0 <= int(emc[1]) <= 100:
        metrics['emc_percent_at_current_frequency'].add(int(emc[1]))
        metrics['emc_mhz'].add(int(emc[2]))
      if gpu and 0 <= int(gpu[1]) <= 100:
        metrics['gpu_active_percent'].add(int(gpu[1]))
  return {key: value.result() for key, value in metrics.items()}


def compare_reports(paths):
  """Side-by-side observations, never a model accuracy ranking."""
  rows = []
  for path in paths:
    with path.open(encoding='utf-8') as stream:
      report = json.load(stream)
    if report.get('schema') != 1 or not isinstance(report.get('metrics'), dict):
      raise ValueError('Not a PV5 drive report')
    rows.append({'label': report.get('label', 'unspecified'), 'input_sha256': report.get('input_sha256', []),
                 'observed_s': report['observed_s'], 'metrics': report['metrics'], 'center': report['center']})
  return {'scope': 'Recorded observations only. Equal image inputs and ground-truth labels were not verified.',
          'same_image_sequence_verified': False, 'accuracy_ranking': None, 'reports': rows}


def main(argv=None):
  sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('logs', type=Path, nargs='*', help='Ordered local full-schema rlog segments')
  parser.add_argument('--output', required=True, type=Path)
  parser.add_argument('--label', default='unspecified', help='Manual model/scene label; not verified model identity')
  parser.add_argument('--tegrastats', type=Path, help='Existing offline Jetson statistics file, if available')
  parser.add_argument('--compare', type=Path, nargs='+', help='Compare previously generated report.json files')
  args = parser.parse_args(argv)
  if Path('/TICI').exists() or Path('/etc/nv_tegra_release').exists():
    raise RuntimeError('Run this offline analyzer on a PC, not comma/Jetson')
  if args.compare:
    if args.logs or args.tegrastats:
      parser.error('--compare accepts saved reports only')
    comparison = compare_reports(args.compare)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'comparison.json').write_text(json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Comparison saved; image-input equivalence and detection accuracy are unverified')
    return 0
  if not args.logs or len(args.label) > 64 or any(ord(c) < 32 for c in args.label):
    parser.error('Provide rlogs and a label of at most 64 printable characters')
  analyzer = DriveReport()
  hashes = []
  for path in args.logs:
    with path.open('rb') as stream:
      hashes.append(hashlib.file_digest(stream, 'sha256').hexdigest())
    for event in read_events(path):
      analyzer.feed(*event)
  report = analyzer.result()
  report['label'] = args.label
  report['input_sha256'] = hashes
  if args.tegrastats:
    report['offline_tegrastats'] = tegrastats_metrics(args.tegrastats)
    report['limits'].append('Tegrastats is summarized separately, without cross-host timestamp correlation or a saturation diagnosis.')
  args.output.mkdir(parents=True, exist_ok=True)
  (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
  (args.output / 'report.md').write_text(markdown(report), encoding='utf-8')
  print(f"PV5={report['pv5_identified']} observed={report['observed_s']:.1f}s; report.json / report.md saved")
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
