#!/usr/bin/env python3
"""Compare CPU staging only; no CUDA engine, USB connection or model publication.

On a Jetson this refuses to compete with an installed live inference service.
An x86 result cannot establish ARM speed, 20 Hz or driving safety.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import tracemalloc

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'third_party/jetlink'))
from jetlink.packed_codec import MAP_PIXELS, _layout, decode, encode
from jetlink.input_cast import native_u8_to_f16
from jetlink.queues import make_queues
from jetlink.server.platform import is_jetson
from jetlink.spec import ModelSpec


def require_offline_host():
  if not is_jetson():
    return
  result = subprocess.run(['systemctl', 'show', 'carrot-jetlink.service', '-p', 'LoadState', '-p', 'ActiveState'],
                          check=True, capture_output=True, text=True, timeout=5)
  fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
  if fields.get('LoadState') != 'loaded' or fields.get('ActiveState') not in ('inactive', 'failed'):
    raise RuntimeError('Stop the installed inference service before an isolated Jetson staging comparison')


def summarize(samples):
  rows = np.asarray(samples) * 1000
  return {'mean_ms': float(rows.mean()), 'p50_ms': float(np.median(rows)),
          'p95_ms': float(np.percentile(rows, 95)), 'p99_ms': float(np.percentile(rows, 99)),
          'max_ms': float(rows.max())}


def measure(functions, frames, rounds):
  for fn in functions.values():
    for index in range(30):
      fn(index % 8)
  samples = {name: [] for name in functions}
  batches = []
  for repeat in range(rounds):
    batch = {}
    for name in (list(functions) if repeat % 2 == 0 else list(reversed(functions))):
      rows = []
      for index in range(frames):
        started = time.perf_counter()
        functions[name](index % 8)
        rows.append(time.perf_counter() - started)
      batch[name] = summarize(rows)
      samples[name].extend(rows)
    batches.append(batch)
  peaks = {}
  for name, fn in functions.items():
    tracemalloc.start()
    try:
      fn(0)
      _, peaks[name] = tracemalloc.get_traced_memory()
    finally:
      tracemalloc.stop()
  return {'aggregate': {name: summarize(rows) for name, rows in samples.items()},
          'batches': batches, 'peak_traced_bytes': peaks}


def extra_candidates(spec, frames, rounds):
  convert = native_u8_to_f16()
  if convert is None:
    raise RuntimeError('Native conversion unavailable')
  source = np.arange(spec.warped_nbytes, dtype=np.uint8)
  target = np.empty(source.size, np.float16)
  slices = [slice(i * source.size // 4, (i + 1) * source.size // 4) for i in range(4)]
  with ThreadPoolExecutor(max_workers=4) as pool:
    def four_workers(index):
      jobs = [pool.submit(convert, source[part], target[part]) for part in slices]
      for job in jobs:
        job.result()
    workers = measure({'one_worker': lambda index: convert(source, target), 'four_workers': four_workers}, frames, rounds)
  if target.tobytes() != source.astype(np.float16).tobytes():
    raise AssertionError('CPU worker conversion differs')
  raw = memoryview(source).cast('B')
  reused = memoryview(bytearray(len(raw)))
  def reusable_copy(index):
    reused[:] = raw
  copies = measure({'allocate_payload': lambda index: bytes(raw), 'reuse_payload': reusable_copy}, frames, rounds)
  if bytes(reused) != bytes(raw):
    raise AssertionError('Reused payload differs')
  return {'cpu_workers': workers, 'payload_copy_only': copies,
          'note': 'Isolated CPU loops; does not implement a live receiver pool or pipeline'}


def compare(spec, frames, rounds):
  rng = np.random.default_rng(20261019)
  images = [rng.integers(0, 256, spec.warped_shape, np.uint8) for _ in range(8)]
  packed = [rng.uniform(-2, 2, spec.packed_nelem).astype(np.float32) for _ in range(8)]
  at = _layout(spec) // 4
  for index, value in enumerate(packed):
    value[0] = -0.0
    value[at:at + MAP_PIXELS] = (0 if index == 0 else np.arange(MAP_PIXELS, dtype=np.uint8)
                                if index == 1 else rng.integers(0, 256, MAP_PIXELS, np.uint8))
  payload = [encode(value, spec)[0] for value in packed]
  results = {}
  for profile in ('mixed', 'float16', 'float32'):
    def dtype(name, profile=profile):
      return np.float16 if profile == 'float16' or (profile == 'mixed' and name in ('new_img', 'state_img_q')) else np.float32
    reference = {name: np.zeros(shape, dtype(name)) for name, shape in spec.input_shapes.items()}
    candidate = {name: np.zeros_like(value) for name, value in reference.items()}
    queues = make_queues(spec, reference)
    fast = make_queues(spec, candidate, input_fastpath=True)
    def original(index, queues=queues, reference=reference):
      queues.step_into(images[index], np.frombuffer(decode(payload[index], spec), np.float32), reference)
    def direct(index, fast=fast, candidate=candidate):
      fast.step_compact_into(images[index], payload[index], candidate)
    for index in range(8):
      original(index)
      direct(index)
      for name in ('new_img', *spec.packed_shapes):
        if reference[name].tobytes() != candidate[name].tobytes():
          raise AssertionError(f'Staging bits differ: {profile}/{name}/{index}')
    functions = {'baseline': original, 'candidate': direct}
    results[profile] = {**measure(functions, frames, rounds), 'inputs_exact': True,
                        'binding_dtypes': {name: str(value.dtype) for name, value in reference.items()}}
  return {'scope': 'CPU input preparation only; excludes GPU inference, physical USB and vehicle operation',
          'platform': platform.platform(), 'numpy': np.__version__, 'model_sha256': spec.sha256,
          'python': sys.version.split()[0],
          'frames_per_batch': frames, 'rounds': rounds, 'results': results,
          'extra_candidates': extra_candidates(spec, frames, rounds),
          'live_orin_validation': False, 'twenty_hz_established': False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--spec', type=Path, default=ROOT / 'openpilot/selfdrive/modeld/jetlink/jetson_model.json')
  parser.add_argument('--frames', type=int, default=400)
  parser.add_argument('--rounds', type=int, default=5)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  if not 100 <= args.frames <= 2000 or not 3 <= args.rounds <= 11 or args.output.exists():
    parser.error('Use 100..2000 frames, 3..11 rounds and a new output file')
  require_offline_host()
  spec = ModelSpec.from_dict(json.loads(args.spec.read_text(encoding='utf-8')))
  if not spec.stateful or 'nav_map' not in spec.packed_shapes:
    parser.error('This comparison requires the existing stateful navigation contract')
  result = compare(spec, args.frames, args.rounds)
  with args.output.open('x', encoding='utf-8') as stream:
    json.dump(result, stream, indent=2)
  print(json.dumps({profile: row['aggregate'] for profile, row in result['results'].items()}))


if __name__ == '__main__':
  main()
