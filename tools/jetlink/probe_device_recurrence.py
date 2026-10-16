"""Activation-only output comparison; one TensorRT process resident at a time."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run_mode(cache, spec, output):
  import numpy as np
  root = Path(__file__).resolve().parents[2]
  sys.path[:0] = [str(root), str(root / 'third_party/jetlink')]
  from jetlink.server.backends.trt import TrtBackend
  from jetlink.server.cache import EngineCache
  from jetlink.server.session import EngineHost, Request
  enabled = os.environ['CARROT_TRT_DEVICE_STATE']
  print('DEVICE_RECURRENCE_MODE', enabled, flush=True)
  host = EngineHost(EngineCache(Path(cache), TrtBackend()))
  values = []
  try:
    host.request(Request(spec['sha256'], spec['nbytes'], spec['frame_skip']), None)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
      status = host.status(spec['sha256'], spec['frame_skip'])
      if status['state'] == 'ready':
        break
      if status['state'] not in ('building', 'loading'):
        raise RuntimeError(str(status))
      time.sleep(.2)
    else:
      raise TimeoutError('State parity engine preparation timed out')
    loaded = host.loaded
    if (loaded.engine.device_state is not None) != (enabled == '1'):
      raise ValueError('Requested state mode was not activated')
    rng = np.random.default_rng(1637)
    for frame in range(32):
      if frame in (0, 16):
        loaded.queues.reset()
      image = rng.integers(0, 256, loaded.spec.warped_shape, dtype=np.uint8)
      packed = np.zeros(loaded.spec.packed_nelem, np.float32)
      loaded.queues.step_into(image, packed, loaded.host_inputs)
      result = loaded.engine.run()
      if not result or not all(np.isfinite(v).all() for v in result.values()):
        raise ValueError('State parity inference is not finite')
      values.append(result['outputs'].copy())
      loaded.queues.feedback(result)
    np.save(output, np.stack(values), allow_pickle=False)
  finally:
    host.close()
  # Exit releases TensorRT contexts/plans and CUDA allocations before the next mode.


def check(cache, spec):
  import numpy as np
  with tempfile.TemporaryDirectory(prefix='carrot-state-parity-') as directory:
    work = Path(directory)
    (work / 'spec.json').write_text(json.dumps(spec))
    for enabled in ('0', '1'):
      subprocess.run([sys.executable, str(Path(__file__).resolve()), str(cache),
                      str(work / 'spec.json'), str(work / (enabled + '.npy'))],
                     env={**os.environ, 'CARROT_TRT_DEVICE_STATE': enabled}, check=True, timeout=920)
    reference = np.load(work / '0.npy', allow_pickle=False)
    candidate = np.load(work / '1.npy', allow_pickle=False)
    if reference.shape != candidate.shape or len(reference) != 32:
      raise ValueError('State parity output shape mismatch')
    if not np.isfinite(reference).all() or not np.isfinite(candidate).all():
      raise ValueError('State parity output is not finite')
    np.testing.assert_allclose(candidate, reference, rtol=1e-4, atol=1e-4,
                               err_msg='GPU state feedback output differs')
    difference = float(np.max(np.abs(candidate.astype(np.float64) - reference)))
    print('DEVICE_RECURRENCE_PARITY_OK', 32, 'frames; reset at 0,16; max_abs', difference, flush=True)


if __name__ == '__main__':
  run_mode(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3]))
