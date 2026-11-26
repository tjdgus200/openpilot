"""Build and smoke-test the exact YOLOv5n v7 FP16 plan on the target GPU."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

MODEL_BYTES = 3_981_910
MODEL_SHA256 = '04f0e55c26f58d17145b36045780fe1250d5bd2187543e11568e5141d05b3262'
MODEL_URL = 'https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx'


def verified_model(model: Path) -> bytes:
  with model.open('rb') as f:
    data = f.read(MODEL_BYTES + 1)
  if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
    raise ValueError(f'Expected original YOLOv5n v7.0 from {MODEL_URL}, SHA256 {MODEL_SHA256}')
  return data


def build(model: Path, output: Path, trtexec: str) -> None:
  data = verified_model(model)
  # The verified snapshot is exactly the file trtexec parses, even if the source path changes.
  with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
    snapshot = Path(tmp) / 'yolov5n.onnx'
    snapshot.write_bytes(data)
    plan = Path(tmp) / 'yolov5n.plan'
    subprocess.run([trtexec, f'--onnx={snapshot}', f'--saveEngine={plan}', '--fp16', '--noTF32',
                    '--inputIOFormats=fp16:chw', '--outputIOFormats=fp16:chw',
                    '--memPoolSize=workspace:256', '--maxAuxStreams=0', '--skipInference'], check=True)
    if not 0 < plan.stat().st_size <= 128 << 20:
      raise ValueError('Unexpected serialized YOLOv5n engine size')
    subprocess.run([trtexec, f'--loadEngine={plan}', '--iterations=1', '--warmUp=0', '--duration=0'], check=True)
    plan.replace(output)
    print(f'{output}  sha256={hashlib.sha256(output.read_bytes()).hexdigest()}')


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('onnx', type=Path)
  parser.add_argument('plan', type=Path)
  parser.add_argument('--trtexec', default=shutil.which('trtexec') or '/usr/src/tensorrt/bin/trtexec')
  args = parser.parse_args()
  if args.onnx.resolve() == args.plan.resolve():
    parser.error('Engine and source model paths must differ')
  build(args.onnx, args.plan, args.trtexec)
