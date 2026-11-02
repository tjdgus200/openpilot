"""Manual PC-only comparison against exported baseline sources; no device access."""
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools/jetlink')]
from openpilot.common import jetlink_status as status
from openpilot.selfdrive.carrot.carrot_navi_control import CarrotNaviReader
import hud_protocol


def load(path, name):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  sys.modules[name] = module
  spec.loader.exec_module(module)
  return module


def measure(before, after, rounds=15, batch=100):
  samples = [[], []]
  for _ in range(rounds):
    for index, call in enumerate((before, after)):
      start = time.perf_counter_ns()
      for _ in range(batch):
        call()
      samples[index].append((time.perf_counter_ns() - start) / batch / 1000.)
  old, new = (statistics.median(values) for values in samples)
  return {'before_us': old, 'after_us': new, 'reduction_percent': 100 * (old - new) / old}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--baseline', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  old_status = load(args.baseline / 'baseline_status.py', 'baseline_status')
  old_hud = load(args.baseline / 'baseline_hud.py', 'baseline_hud')
  old_navi = load(args.baseline / 'baseline_navi.py', 'baseline_navi')
  result = {'scope': 'Linux x86 PC microbenchmark; not C3/Jetson CPU, inference latency or road-safety proof',
            'baseline_commit': '120566c473fd117a558d216b2dd525eb0d2bdcac'}
  with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='comma-packet-bench-') as name:
    now = time.monotonic()
    link_path, model_path = Path(name) / 'link.json', Path(name) / 'model.json'
    link_path.write_text(json.dumps({'updated': now, 'state': 'ready', 'peer': {'carrot_host': 'jetson'},
      'telemetry_updated': now, 'telemetry': {'carrot_health': {'age_s': 0., 'severity': 'ok', 'temp_c': 60.}}}))
    model_path.write_text(json.dumps({'updated': now, 'active': True}))
    saved = status.LINK_STATUS, status.MODEL_STATUS, status.time.monotonic
    try:
      status.LINK_STATUS = old_status.LINK_STATUS = link_path
      status.MODEL_STATUS = old_status.MODEL_STATUS = model_path
      status.time.monotonic = lambda: now
      def before_status():
        return old_status.badge(), old_status._fresh(link_path, now)
      def after_status():
        snapshot = status.read_status()
        return status.badge(snapshot), status.fresh_value(snapshot[0], now)
      assert before_status() == after_status()
      result['status_packet'] = measure(before_status, after_status) | {'file_reads_before': 6, 'file_reads_after': 2}
    finally:
      status.LINK_STATUS, status.MODEL_STATUS, status.time.monotonic = saved

  message = {'schemaVersion': 1, 'connected': True, 'sessionId': 'synthetic',
             'speed': {'meta': {'present': True, 'sequence': 1}, 'roadLimitValid': True, 'roadLimitKph': 80},
             'route': {'meta': {'present': True, 'sequence': 1}, 'polyline': [
               {'latitude': 37. + i * .0001, 'longitude': 127. + i * .0001} for i in range(256)]}}
  class SM:
    alive, valid = {'carrotNavi': True}, {'carrotNavi': True}
    recv_frame, logMonoTime = {'carrotNavi': 0}, {'carrotNavi': 100}
    def __getitem__(self, _):
      return message
  sm, reader = SM(), CarrotNaviReader()
  assert asdict(old_navi.parse_carrot_navi_control(message)) == asdict(reader.read(sm))
  result['navigation'] = {}
  for messages_per_20_ticks in (1, 5, 20):
    interval = 20 // messages_per_20_ticks
    def before_navi(interval=interval):
      for tick in range(20):
        old_navi.parse_carrot_navi_control(message)
        if tick % interval == 0:
          old_navi.parse_carrot_navi_control(message)
    def after_navi(interval=interval):
      for tick in range(20):
        if tick % interval == 0:
          sm.recv_frame['carrotNavi'] += 1
        reader.read(sm)
        if tick % interval == 0:
          reader.read(sm)
    result['navigation'][str(messages_per_20_ticks)] = measure(before_navi, after_navi, batch=10) | {
      'unit': '20 service ticks; synthetic 256-point route',
      'parses_before': 20 + messages_per_20_ticks, 'parses_after': messages_per_20_ticks}

  result['json'] = {}
  for name, value in {
    'small': {'events': {'carState': 'C' * 6000}, 'cameras': {}},
    'signals_only': {'events': {'carState': 'C' * 6000}, 'indicator_display': 'I' * 100000, 'cameras': {}},
    'drop_camera': {'events': {'carState': 'C' * 70000}, 'cameras': {'road': 'C' * 100000}},
    'drop_map': {'events': {'carState': 'C' * 70000}, 'cameras': {'road': 'C' * 20000}, 'nav_map_preview': 'N' * 100000},
    'drop_route': {'events': {'carState': 'C' * 70000, 'navRoute': 'R' * 110000},
                   'cameras': {'road': 'C' * 20000}, 'nav_map_preview': 'N' * 40000},
  }.items():
    value.setdefault('indicator_display', None)
    value.setdefault('signal_display', {'active': False, 'updated': 100., 'side': ''})
    before, after = dict(value), dict(value)
    assert old_hud.encode_snapshot(before) == hud_protocol.encode_snapshot(after) and before == after
    result['json'][name] = measure(lambda value=value: old_hud.encode_snapshot(dict(value)),
                                  lambda value=value: hud_protocol.encode_snapshot(dict(value)), batch=100)
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(result, indent=2) + '\n')
  print(json.dumps(result, indent=2))


if __name__ == '__main__':
  main()
