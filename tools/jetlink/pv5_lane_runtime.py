"""Prepare the optional Xiaoge engine during stopped-service candidate validation."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'openpilot/selfdrive/carrot/xiaoge/assets/lane.onnx'
MODEL_SHA = 'd3761c185daf33897ff3ff5edf28115c5dc829c06c538033accb616d6c67528c'
CACHE = Path('/opt/carrot-jetlink/cache/pv5-lane-review')
# The paired-package exporter verifies and includes this provenance file.
PV5_PACKAGE = ROOT / 'tools/pv5_radar_labels/runtime/notices/provenance.json'
PLAN = 'xiaoge-lane-fp32-v1.plan'
SHAPES = {'output0': (1, 42, 3549), 'output1': (1, 32, 104, 104)}


def _plan_record(cache, marker_path, release=None):
  try:
    marker = json.loads(marker_path.read_text())
    digest = marker['plan']
    if (marker['model'] == MODEL_SHA and re.fullmatch('[a-f0-9]{40}', marker['release'])
        and (release is None or release == marker['release']) and re.fullmatch('[a-f0-9]{64}', digest)
        and type(marker['bytes']) is int and 0 < marker['bytes'] <= 128 << 20):
      # The original bench cache used a single fixed filename.
      plan = cache / (digest + '.plan' if marker.get('format') == 2 else PLAN)
      if plan.stat().st_size == marker['bytes']:
        return plan, digest
  except (OSError, ValueError, KeyError, TypeError):
    pass
  return None


def installed_plan(cache=CACHE):
  try:
    commit = (ROOT / 'SOURCE_COMMIT').read_text().strip()
  except OSError:
    return None
  if not re.fullmatch('[a-f0-9]{40}', commit):
    return None
  return (_plan_record(cache, cache / (commit + '.json'), commit)
          or _plan_record(cache, cache / 'validated.json', commit))


def configure_installed(cache=CACHE):
  # The flag configures the primary TRT stream's priority as well as HELLO.
  # Only a negotiated per-frame request can load or run the optional predictor.
  if 'PV5_LANE_REVIEW' not in os.environ and PV5_PACKAGE.is_file() and installed_plan(cache) is not None:
    os.environ['PV5_LANE_REVIEW'] = '1'


class Predictor:
  def __init__(self, path, digest, device=0):
    import numpy as np
    from jetlink.server.backends.trt.engine import TrtEngine
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
      raise ValueError('Lane plan digest mismatch')
    engine = TrtEngine(str(path), device=device, background=True)
    try:
      if (list(engine.input_shapes.values()) != [(1, 3, 416, 416)] or engine.output_shapes != SHAPES
          or any(b.dtype != np.float32 for b in engine.bindings.values())):
        raise ValueError('Unexpected Xiaoge tensor contract')
      self.input = engine.host_input(next(iter(engine.inputs)))
      self.engine = engine
    except BaseException:
      engine.close()
      raise

  def __call__(self, blob):
    self.input[:] = blob
    return self.engine.run()

  def close(self):
    self.engine.close()


def _build(model, path):
  import tensorrt as trt
  logger = trt.Logger(trt.Logger.WARNING)
  builder = trt.Builder(logger)
  net = builder.create_network(0)
  parser = trt.OnnxParser(net, logger)
  if not parser.parse(model):
    raise ValueError('TensorRT could not parse the pinned lane ONNX')
  config = builder.create_builder_config()
  config.clear_flag(trt.BuilderFlag.TF32)
  config.max_aux_streams = 0
  config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 256 << 20)
  plan = builder.build_serialized_network(net, config)
  data = bytes(plan) if plan is not None else b''
  if not 0 < len(data) <= 128 << 20:
    raise ValueError('Invalid serialized Xiaoge engine')
  path.write_bytes(data)


def _smoke(path, digest):
  import numpy as np
  predictor = Predictor(path, digest)
  try:
    result = predictor(np.zeros((1, 3, 416, 416), np.float32))
    if not all(np.isfinite(v).all() for v in result.values()):
      raise ValueError('Nonfinite Xiaoge smoke output')
  finally:
    predictor.close()


def prepare(cache=CACHE):
  for unit in ('carrot-jetlink.service', 'carrot-jetlink-hud.service'):
    status = subprocess.run(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState'],
                            check=True, text=True, capture_output=True, timeout=5)
    fields = dict(row.split('=', 1) for row in status.stdout.splitlines() if '=' in row)
    if fields.get('LoadState') != 'loaded' or fields.get('ActiveState') not in ('inactive', 'failed'):
      raise RuntimeError('Lane plan preparation requires stopped inference and HUD services')
  commit = (ROOT / 'SOURCE_COMMIT').read_text().strip()
  model = MODEL.read_bytes()
  if not re.fullmatch('[a-f0-9]{40}', commit) or hashlib.sha256(model).hexdigest() != MODEL_SHA:
    raise ValueError('Unrecognized release or Xiaoge lane model')
  cache.mkdir(mode=0o700, exist_ok=True, parents=True)
  with tempfile.TemporaryDirectory(dir=cache) as temporary:
    staged = Path(temporary) / PLAN
    previous = installed_plan(cache) or _plan_record(cache, cache / 'validated.json')
    if previous is not None:
      try:
        _smoke(*previous)
      except (OSError, ValueError, RuntimeError):
        previous = None
    if previous is None:
      _build(model, staged)
      digest = hashlib.sha256(staged.read_bytes()).hexdigest()
      _smoke(staged, digest)
      os.replace(staged, cache / (digest + '.plan'))
    else:
      path, digest = previous
      if path.name == PLAN:
        staged.write_bytes(path.read_bytes())
        os.replace(staged, cache / (digest + '.plan'))
    size = (cache / (digest + '.plan')).stat().st_size
    marker = json.dumps({'format': 2, 'release': commit, 'model': MODEL_SHA, 'plan': digest, 'bytes': size})
    # Preserve the previous release's validated engine across a rejected update.
    for name in (commit + '.json', 'validated.json'):
      record = Path(temporary) / name
      record.write_text(marker)
      os.replace(record, cache / name)


def prepare_optional(cache, deadline):
  remaining = deadline - time.monotonic()
  if remaining < 1 or not PV5_PACKAGE.is_file() or not MODEL.is_file():
    return False
  try:
    # A CUDA/parser crash or timeout in this process must not reject the driving model.
    subprocess.run([sys.executable, __file__, 'prepare', str(cache / CACHE.name)],
                   check=True, timeout=min(remaining, 180))
    return True
  except (OSError, subprocess.SubprocessError) as exc:
    print(f'Optional lane review unavailable: {exc}', file=sys.stderr, flush=True)
    return False


if __name__ == '__main__':
  if sys.argv[1:2] != ['prepare'] or len(sys.argv) > 3:
    raise SystemExit('Usage: pv5_lane_runtime.py prepare [cache-directory]')
  sys.path[:0] = [str(ROOT), str(ROOT / 'third_party/jetlink')]
  prepare(Path(sys.argv[2]) if len(sys.argv) == 3 else CACHE)
