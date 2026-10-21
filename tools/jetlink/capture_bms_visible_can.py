"""Bounded, receive-only CAN visibility check. Run on a parked comma device.

No diagnostic requests, flow-control packets, Bluetooth, or control changes.
Only candidate BMS addresses are retained; other CAN data is counted, not saved.
This does not decode or validate a PV5 battery temperature.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import time


TARGET_IDS = frozenset((0x7E4, 0x7EC))
MAX_SAMPLES = 2048
MAX_BUS_KEYS = 32


class Capture:
  def __init__(self):
    self.events = 0
    self.invalid_events = 0
    self.total_frames = 0
    self.buses = Counter()
    self.targets = Counter()
    self.samples = []
    self.dropped_samples = 0
    self.untracked_bus_frames = 0

  def consume(self, event, elapsed):
    self.events += 1
    if not event.valid:
      self.invalid_events += 1
      return
    for frame in event.can:
      self.total_frames += 1
      bus = int(frame.src)
      # Panda's returned TX packets use src >= 128. Keep them out of the
      # received-bus evidence, even if published on the same can stream.
      if bus >= 128:
        continue
      if bus not in self.buses and len(self.buses) >= MAX_BUS_KEYS:
        self.untracked_bus_frames += 1
        continue
      self.buses[bus] += 1
      address = int(frame.address)
      if address not in TARGET_IDS:
        continue
      self.targets[(bus, address)] += 1
      if len(self.samples) >= MAX_SAMPLES:
        self.dropped_samples += 1
        continue
      raw = bytes(frame.dat)
      if not 0 < len(raw) <= 64:
        continue
      self.samples.append({
        'elapsed_s': round(elapsed, 6), 'bus': bus,
        'address': f'0x{address:03X}', 'data_hex': raw.hex(),
      })

  def result(self, duration, interrupted):
    return {
      'version': 1, 'mode': 'receive_only', 'duration_s': round(duration, 3),
      'interrupted': interrupted, 'events': self.events,
      'invalid_events_excluded': self.invalid_events,
      'total_frames_including_echoes': self.total_frames,
      'received_bus_counts': {str(bus): count for bus, count in sorted(self.buses.items())},
      'candidate_counts': [
        {'bus': bus, 'address': f'0x{address:03X}', 'count': count}
        for (bus, address), count in sorted(self.targets.items())
      ],
      'samples': self.samples, 'sample_limit': MAX_SAMPLES,
      'samples_dropped_at_limit': self.dropped_samples,
      'untracked_bus_frames': self.untracked_bus_frames,
      'temperature_verified': False,
      'interpretation': 'Address presence alone does not confirm a BMS DID or PV5 temperature formula.',
    }


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--seconds', type=int, choices=range(1, 121), default=60, metavar='1..120')
  parser.add_argument('--output', type=Path, required=True, help='New local JSON file; never overwritten')
  args = parser.parse_args()
  if args.output.exists():
    parser.error('Output already exists; choose a new file')
  # A separate, low-priority reader; never add this to the driving frame loop.
  os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  os.nice(10)
  from openpilot.cereal import messaging
  can_socket = messaging.sub_sock('can', conflate=False, timeout=100)
  capture = Capture()
  interrupted = False
  start = time.monotonic()
  deadline = start + args.seconds
  try:
    while time.monotonic() < deadline:
      event = messaging.recv_one(can_socket)
      if event is not None:
        capture.consume(event, time.monotonic() - start)
  except KeyboardInterrupt:
    interrupted = True
  result = capture.result(time.monotonic() - start, interrupted)
  with args.output.open('x', encoding='utf-8') as output:
    json.dump(result, output, separators=(',', ':'))
    output.write('\n')
  # Keep raw payload off terminal/Discord. It stays in the local evidence file.
  print(json.dumps({
    'duration_s': result['duration_s'],
    'received_frames': sum(capture.buses.values()),
    'candidate_counts': result['candidate_counts'],
    'samples_saved': len(capture.samples),
    'temperature_verified': False,
  }))


if __name__ == '__main__':
  main()
