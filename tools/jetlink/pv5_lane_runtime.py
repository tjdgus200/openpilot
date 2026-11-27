"""Explicit stopped-service lab preparation of a GPU-only Xiaoge lane classifier."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'openpilot/selfdrive/carrot/xiaoge/assets/lane.onnx'
MODEL_SHA = 'd3761c185daf33897ff3ff5edf28115c5dc829c06c538033accb616d6c67528c'
CACHE = Path('/opt/carrot-jetlink/cache/pv5-lane-review')
PLAN = 'xiaoge-lane-fp32-v1.plan'
SHAPES = {'output0': (1, 42, 3549), 'output1': (1, 32, 104, 104)}


def installed_plan(cache=CACHE):
  if os.environ.get('PV5_LANE_REVIEW') != '1':
    return None
  try:
    commit = (ROOT / 'SOURCE_COMMIT').read_text().strip()
    marker = json.loads((cache / 'validated.json').read_text())
    plan = cache / PLAN
    if (re.fullmatch('[a-f0-9]{40}', commit) and marker['release'] == commit and marker['model'] == MODEL_SHA
        and re.fullmatch('[a-f0-9]{64}', marker['plan']) and plan.stat().st_size == marker['bytes']):
      return plan, marker['plan']
  except (OSError, ValueError, KeyError, TypeError):
    pass
  return None


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


def prepare(cache=CACHE):
  # This is an explicit lab CLI, never an onroad startup or install hook.
  status = subprocess.run(['systemctl', 'show', 'carrot-jetlink.service', '-p', 'LoadState', '-p', 'ActiveState'],
                          check=True, text=True, capture_output=True, timeout=5)
  fields = dict(row.split('=', 1) for row in status.stdout.splitlines() if '=' in row)
  if fields.get('LoadState') != 'loaded' or fields.get('ActiveState') not in ('inactive', 'failed'):
    raise RuntimeError('Prepare only on a laboratory Jetson with the installed inference service stopped')
  import numpy as np
  import tensorrt as trt
  commit = (ROOT / 'SOURCE_COMMIT').read_text().strip()
  model = MODEL.read_bytes()
  if not re.fullmatch('[a-f0-9]{40}', commit) or hashlib.sha256(model).hexdigest() != MODEL_SHA:
    raise ValueError('Unrecognized release or Xiaoge lane model')
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
  if plan is None:
    raise RuntimeError('Failed to build Xiaoge TensorRT plan')
  cache.mkdir(mode=0o700, exist_ok=True, parents=True)
  with tempfile.TemporaryDirectory(dir=cache) as temporary:
    staged = Path(temporary) / PLAN
    staged.write_bytes(bytes(plan))
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    predictor = Predictor(staged, digest)
    try:
      result = predictor(np.zeros((1, 3, 416, 416), np.float32))
      if not all(np.isfinite(v).all() for v in result.values()):
        raise ValueError('Nonfinite Xiaoge smoke output')
    finally:
      predictor.close()
    marker = Path(temporary) / 'validated.json'
    marker.write_text(json.dumps({'release': commit, 'model': MODEL_SHA, 'plan': digest, 'bytes': staged.stat().st_size}))
    os.replace(staged, cache / PLAN)
    os.replace(marker, cache / 'validated.json')


if __name__ == '__main__':
  if sys.argv[1:] != ['prepare']:
    raise SystemExit('Usage: pv5_lane_runtime.py prepare')
  sys.path[:0] = [str(ROOT), str(ROOT / 'third_party/jetlink')]
  prepare()
