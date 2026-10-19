#!/usr/bin/env python3
"""Discrete-event scheduling illustration, not a vehicle/model or Orin simulator.

All times are explicit synthetic assumptions. No physical timing, actual model
outputs or validity is inferred. The real runtime and its ordering are untouched.
"""
import argparse
import heapq
import itertools
import json
from pathlib import Path

import numpy as np


def simulate(pre_ms, gpu_ms, post_ms, *, pipelined, period_ms=50):
  count = len(gpu_ms)
  events, token = [], itertools.count()
  finished = []
  def schedule(stamp, callback, frame):
    heapq.heappush(events, (stamp, next(token), callback, frame))

  class Worker:
    def __init__(self, duration, following):
      self.duration, self.following = duration, following
      self.busy, self.waiting, self.dropped = False, None, 0
    def accept(self, now, frame):
      if self.busy:
        self.dropped += int(self.waiting is not None)
        self.waiting = frame  # Bound pending work to the latest single frame.
      else:
        self.busy = True
        schedule(now + self.duration(frame), self.complete, frame)
    def complete(self, now, frame):
      self.following(now, frame)
      self.busy = False
      if self.waiting is not None:
        next_frame, self.waiting = self.waiting, None
        self.accept(now, next_frame)

  def publish(now, frame):
    finished.append((frame, now))
  if pipelined:
    output = Worker(lambda frame: post_ms, publish)
    gpu = Worker(lambda frame: gpu_ms[frame], output.accept)
    entry = Worker(lambda frame: pre_ms, gpu.accept)
    workers = [entry, gpu, output]
  else:
    entry = Worker(lambda frame: pre_ms + gpu_ms[frame] + post_ms, publish)
    workers = [entry]
  for frame in range(count):
    schedule(frame * period_ms, entry.accept, frame)
  while events:
    now, _, callback, frame = heapq.heappop(events)
    callback(now, frame)
  ids = [frame for frame, _ in finished]
  assert ids == sorted(set(ids)), 'Simulation must never relabel old results or duplicate frames'
  times = np.asarray([stamp for _, stamp in finished])
  ages = np.asarray([stamp - frame * period_ms for frame, stamp in finished])
  intervals = np.diff(times)
  return {'produced': len(ids), 'source_frames': count, 'dropped_before_output': count - len(ids),
          'mean_hz': float((len(times) - 1) * 1000 / (times[-1] - times[0])),
          'age_mean_ms': float(ages.mean()), 'age_p99_ms': float(np.percentile(ages, 99)),
          'age_max_ms': float(ages.max()), 'interval_p99_ms': float(np.percentile(intervals, 99)),
          'interval_max_ms': float(intervals.max()), 'worker_drops': [worker.dropped for worker in workers]}


def scenarios(frames=12000):
  rng = np.random.default_rng(20261019)
  steady = np.full(frames, 25.0)
  bursty = steady.copy()
  bursty[rng.random(frames) < .03] += 40
  result = {}
  for name, pre, gpu, post in [('steady', 12.35, steady, 4), ('rare_gpu_stalls', 12.35, bursty, 4),
                                ('cpu_bound_example', 40, steady, 4),
                                ('gpu_bound_example', 12.35, np.full(frames, 55.0), 4)]:
    result[name] = {'assumptions': {'pre_ms': pre, 'post_ms': post,
                                  'gpu_base_ms': float(np.min(gpu)), 'gpu_max_ms': float(np.max(gpu)),
                                  'source_period_ms': 50, 'pending_frames_per_stage': 1},
                    'serial': simulate(pre, gpu, post, pipelined=False),
                    'serial_with_0_24ms_preparation_saving': simulate(pre - .24, gpu, post, pipelined=False),
                    'hypothetical_pipeline': simulate(pre, gpu, post, pipelined=True)}
  return {'scope': 'Synthetic event scheduling only; not actual model/state, hardware or driving validation',
          'frames_per_scenario': frames, 'timings_are_assumptions': True, 'scenarios': result,
          'model_safety_established': False, 'production_pipeline_changed': False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  with args.output.open('x', encoding='utf-8') as stream:
    result = scenarios()
    json.dump(result, stream, indent=2)
  print(json.dumps(result))


if __name__ == '__main__':
  main()
