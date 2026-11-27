"""Replay the Jetson lane reviewer on labelled, frame-matched PC fixtures.

The JSON manifest is a list of {case, side, phase, relative_ms, truth, stratum, file}.
Each file is an NPZ containing road_y (256x512 uint8), side (1/2), line_yz
(33x2) and probability from the same road frame. Missing input rows may carry
"skip" instead of "file". JSON output contains no images or archive paths.
Run as python -m tools.pv5_archive.replay_lane_review MANIFEST --out report.json.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from time import perf_counter_ns

import cv2
import numpy as np

from openpilot.selfdrive.carrot.xiaoge.lane_inference import DEFAULT_LANE_MODEL_PATH
from tools.jetlink.pv5_lane_review import classify


def evaluate(manifest, model):
  cv2.setNumThreads(2)
  network = cv2.dnn.readNetFromONNX(str(model))
  names = network.getUnconnectedOutLayersNames()
  calls = 0

  def forward(blob):
    nonlocal calls
    calls += 1
    network.setInput(blob)
    return dict(zip(names, network.forward(names), strict=True))

  groups = defaultdict(Counter)
  results = []
  times = []
  for record in json.loads(manifest.read_text(encoding='utf-8')):
    row = {key: record[key] for key in ('case', 'side', 'phase', 'relative_ms', 'truth', 'stratum')}
    results.append(row)
    if 'skip' in record:
      row['skip'] = record['skip']
      groups[row['stratum']]['skipped'] += 1
      continue
    with np.load(manifest.parent / record['file'], allow_pickle=False) as data:
      plane, coords = data['road_y'], data['line_yz']
      side, probability = int(data['side']), float(data['probability'])
    previous_calls = calls
    start = perf_counter_ns()
    kind, confidence = classify(plane, side, coords, probability, forward)
    elapsed = (perf_counter_ns() - start) / 1e6
    inferred = calls != previous_calls
    if inferred:
      times.append(elapsed)
    prediction = {-1: 'unknown', 0: 'dashed', 1: 'solid'}[kind]
    row.update(prediction=prediction, confidence=round(confidence, 4), inferred=inferred,
               lane_probability=round(probability, 4), pc_ms=round(elapsed, 2))
    if 'camera_to_model_ms' in record:
      row['recorded_model_publication_ms_after_eof'] = record['camera_to_model_ms']
    expected = row['truth']
    groups[row['stratum']][f'{expected}/{prediction}'] += 1
  return dict(model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(), opencv=cv2.__version__, calls=calls,
              pc_classify_ms={f'p{q}': round(float(np.percentile(times, q)), 2) for q in (50, 95, 100)} if times else {},
              counts=groups, rows=results)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('manifest', type=Path)
  parser.add_argument('--model', type=Path, default=DEFAULT_LANE_MODEL_PATH)
  parser.add_argument('--out', required=True, type=Path)
  args = parser.parse_args()
  report = evaluate(args.manifest, args.model)
  args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
  print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, indent=2))


if __name__ == '__main__':
  main()
