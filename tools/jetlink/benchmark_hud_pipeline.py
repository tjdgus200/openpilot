"""Offline PC microbenchmarks + assumed HUD-stage model, never run by services.

No device/driver/model operations. Stage values are sensitivity assumptions,
not Jetson measurements. Reports throughput and frame age separately.
"""
import argparse
import heapq
import json
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'openpilot/selfdrive/carrot/cluster')]


def summary(values):
  ordered = sorted(values)
  return {'mean': statistics.mean(values), 'p99': ordered[min(len(ordered)-1, int(.99*len(ordered)))],
          'maximum': max(values)}


def simulate(render_ms, encode_ms, usb_ms, *, asynchronous, copy_ms=.5, contention=0.,
             seconds=60., stall_every=0, stall_ms=0., seed=72):
  """Non-overlapping renderer, one encoder/sender, one replaceable pending frame."""
  rng = random.Random(seed)
  period, end = 50., seconds * 1000
  events, pending, outputs = [], None, []
  busy, dropped, sequence = False, 0, 0
  def costs():
    nonlocal sequence
    sequence += 1
    factor = 1 + contention if asynchronous else 1
    r = render_ms * rng.uniform(.9, 1.1) * factor
    e = encode_ms * rng.uniform(.9, 1.1) * factor
    u = usb_ms * rng.uniform(.9, 1.1)
    if stall_every and sequence % stall_every == 0:
      u += stall_ms
    return r, e, u
  if not asynchronous:
    start = 0.
    while start < end:
      r, e, u = costs()
      finish = start + r + e + u
      if finish <= end:
        outputs.append(finish-start)
      # Existing loop sleeps the remaining frame period, never catches up.
      start += max(period, finish-start)
  else:
    heapq.heappush(events, (0., 'produce', None))
    def dispatch(now, frame):
      nonlocal busy, dropped
      born, staged, e, u = frame
      if now - staged >= 150. or now + e - staged >= 150.:
        dropped += 1
        return
      busy = True
      heapq.heappush(events, (now + e + u, 'complete', born))
    while events:
      now, kind, data = heapq.heappop(events)
      if now > end:
        break
      if kind == 'produce':
        r, e, u = costs()
        finish = now + r + copy_ms
        heapq.heappush(events, (finish, 'staged', (now, finish, e, u)))
        heapq.heappush(events, (now + max(period, r + copy_ms), 'produce', None))
      elif kind == 'staged':
        if busy:
          dropped += pending is not None
          pending = data
        else:
          dispatch(now, data)
      else:
        outputs.append(now-data)
        busy = False
        if pending is not None:
          latest, pending = pending, None
          dispatch(now, latest)
  return {'fps': len(outputs)/seconds, 'age_ms': summary(outputs) if outputs else None,
          'pending_replacements_or_expiry': dropped}


def microbenchmarks():
  import hud_protocol as wire
  from cluster_battery import HeatmapLabels, heat_color
  def bench(call, iterations=500):
    for _ in range(20):
      call()
    samples = []
    for _ in range(iterations):
      start = time.perf_counter_ns()
      call()
      samples.append((time.perf_counter_ns()-start)/1000)
    return summary(samples)
  values = [3.96 + .02*(i % 3) if i % 7 else None for i in range(108)]
  def original_labels():
    present = [v for v in values if v is not None]
    reference = min(present)
    return tuple((heat_color(v, reference, 'cell_voltages_v'),
                  f'{i+1:03d}', f'{v:.2f}' if v is not None else '--') for i, v in enumerate(values))
  labels = HeatmapLabels()
  result = {'unit': 'microseconds per call on this PC; not Jetson',
            'heatmap_original': bench(original_labels),
            'heatmap_cached': bench(lambda: labels.get('cell_voltages_v', values))}
  synthetic = {'version': 1, 'sent': 9.9, 'events': {'carState': 'A' * 65536}, 'params': {}}
  packet = wire.HEADER.pack(9.9) + json.dumps(synthetic).encode()
  saved = wire.HUD_PACKET, wire.time, wire._cached_snapshot, wire._snapshot_signature, wire._next_snapshot_read
  try:
    with tempfile.TemporaryDirectory(prefix='hud-pc-cache-') as directory:
      wire.HUD_PACKET = Path(directory) / 'packet'
      wire.HUD_PACKET.write_bytes(packet)
      wire.time = NS(monotonic=lambda: 10.)
      def original_read():
        with wire.HUD_PACKET.open('rb') as stream:
          data = stream.read(wire.MAX_HUD_BYTES + wire.HEADER.size + 1)
        return json.loads(data[wire.HEADER.size:])
      def cached_read():
        wire._next_snapshot_read = 0.
        return wire.read_snapshot()
      result['snapshot_original_64KiB'] = bench(original_read)
      result['snapshot_unchanged_64KiB'] = bench(cached_read)
  finally:
    wire.HUD_PACKET, wire.time, wire._cached_snapshot, wire._snapshot_signature, wire._next_snapshot_read = saved
  # Mandatory ownership copy is retained. Compare per-frame allocation with a
  # preallocated destination; this is not GPU readback or a zero-copy claim.
  source = memoryview(bytearray(1920 * 462 * 4))
  target = bytearray(len(source))
  result['owned_bytes_allocate_1920x462'] = bench(lambda: bytes(source))
  result['owned_buffer_reuse_1920x462'] = bench(lambda: memoryview(target).__setitem__(slice(None), source))
  result['pool_bytes_1920x462'] = 2 * len(source)
  result['pool_bytes_maximum'] = 2 * 1920 * 720 * 4
  return result


def run():
  report = {'scope': 'Offline PC measurements plus assumed stage model; no physical Jetson/USB/road safety proof',
            'microbenchmarks': microbenchmarks(), 'model': []}
  for name, r, e, u, stalls in [('ample_headroom', 8, 10, 5, 0),
                               ('near_50ms_budget', 20, 22, 12, 0),
                               ('slow_usb', 15, 18, 40, 0),
                               ('usb_stalls', 15, 18, 12, 20)]:
    for contention in (0., .1, .2):
      args = {'render_ms': r, 'encode_ms': e, 'usb_ms': u, 'stall_every': stalls, 'stall_ms': 200.}
      before = simulate(**args, asynchronous=False)
      after = simulate(**args, asynchronous=True, contention=contention)
      report['model'].append({'scenario': name, 'assumed_stage_ms': [r, e, u],
                              'assumed_parallel_compute_slowdown': contention,
                              'before': before, 'after': after})
  return report


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  args.output.write_text(json.dumps(run(), indent=2) + '\n', encoding='utf-8')
