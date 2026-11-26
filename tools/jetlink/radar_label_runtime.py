"""Prepare the optional GPU engine during candidate validation, never during a drive."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'tools/pv5_radar_labels/runtime'
CACHE = Path('/opt/carrot-jetlink/cache/pv5-radar-labels')
PLAN_NAME = 'yolov5n-v7-fp16-v1.plan'
LIB = 'libpv5_radar_labels.so'
WORKER = 'radar-label-worker'


def ready_file(cache):
  commit = (ROOT / 'SOURCE_COMMIT').read_text().strip()
  if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
    raise ValueError('Invalid source commit')
  return cache / (commit + '.ready')


def configure_installed():
  # Explicit bench configuration retains its own worker and runtime paths.
  if 'PV5_RADAR_LABELS' in os.environ:
    return
  try:
    if (not ready_file(CACHE).is_file() or not (CACHE / PLAN_NAME).is_file()
        or not (RUNTIME / LIB).is_file() or not os.access(RUNTIME / WORKER, os.X_OK)):
      return
    os.environ.update(PV5_RADAR_LABEL_LIB=str(RUNTIME / LIB), PV5_RADAR_LABEL_PLAN=str(CACHE / PLAN_NAME),
                      PV5_RADAR_LABEL_WORKER=str(RUNTIME / WORKER),
                      PV5_RADAR_LABEL_DIR=f'/dev/shm/pv5-radar-labels-{os.geteuid()}', PV5_RADAR_LABELS='1')
  except (OSError, ValueError):
    return


def prepare_optional(cache, deadline):
  cache = Path(cache) / 'pv5-radar-labels'
  try:
    marker = ready_file(cache)
    cache.mkdir(mode=0o700, exist_ok=True)
    marker.unlink(missing_ok=True)
    if not (RUNTIME / LIB).is_file() or not os.access(RUNTIME / WORKER, os.X_OK):
      raise FileNotFoundError('Packaged ARM64 detector or worker is missing')

    def run(action, path):
      remaining = deadline - time.monotonic()
      if remaining < 1:
        raise TimeoutError('Optional validation time exhausted')
      # A native loader or CUDA failure cannot terminate the primary model probe.
      subprocess.run([sys.executable, __file__, action, str(path)], check=True, timeout=remaining)

    plan = cache / PLAN_NAME
    if plan.is_file():
      try:
        run('check', plan)
        marker.touch()
        return True
      except subprocess.CalledProcessError:
        pass  # Different TensorRT/GPU or incomplete cache; rebuild in isolation.
    with tempfile.TemporaryDirectory(dir=cache) as temp:
      staged = Path(temp) / PLAN_NAME
      run('build', staged)
      run('check', staged)
      os.replace(staged, plan)
    marker.touch()
    return True
  except (OSError, ValueError, subprocess.SubprocessError) as exc:
    print(f'Optional radar labels unavailable: {exc}', file=sys.stderr, flush=True)
    return False


def build_plan(output):
  # The image already includes TensorRT Python for building the driving model.
  # It need not include Rust, CUDA development headers, trtexec or internet.
  import tensorrt as trt
  from tools.pv5_radar_labels.native.build_engine import verified_model
  model = verified_model(RUNTIME / 'yolov5n.onnx')
  logger = trt.Logger(trt.Logger.WARNING)
  builder = trt.Builder(logger)
  network = builder.create_network(0)
  parser = trt.OnnxParser(network, logger)
  if not parser.parse(model) or network.num_inputs != 1 or network.num_outputs != 1:
    raise ValueError('Cannot parse the pinned YOLOv5n network')
  for tensor in (network.get_input(0), network.get_output(0)):
    tensor.dtype = trt.float16
    tensor.allowed_formats = 1 << int(trt.TensorFormat.LINEAR)
  config = builder.create_builder_config()
  config.set_flag(trt.BuilderFlag.FP16)
  config.clear_flag(trt.BuilderFlag.TF32)
  config.max_aux_streams = 0
  config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 256 << 20)
  serialized = builder.build_serialized_network(network, config)
  plan = bytes(serialized) if serialized is not None else b''
  if not 0 < len(plan) <= 128 << 20:
    raise ValueError('Invalid serialized YOLO engine')
  output.write_bytes(plan)


def check_plan(plan):
  from parallel_radar_labels import RustDetector, WIDE_BYTES
  # With no arguments the worker prints usage before touching any socket.
  result = subprocess.run([RUNTIME / WORKER], capture_output=True, timeout=3)
  if result.returncode != 1 or b'usage: radar-label-worker PRIVATE_RUN_DIRECTORY [HUD_PACKET]' not in result.stderr:
    raise RuntimeError('Packaged radar worker failed its loader check')
  detector = RustDetector(RUNTIME / LIB, plan)
  try:
    detector.detect(bytes(WIDE_BYTES))
  finally:
    detector.close()


if __name__ == '__main__':
  sys.path.insert(0, str(ROOT))
  {'build': build_plan, 'check': check_plan}[sys.argv[1]](Path(sys.argv[2]))
