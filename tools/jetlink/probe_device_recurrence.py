"""Activation-only comparison of host/GPU state feedback on the same TRT engine."""
import os
from pathlib import Path
import time


def check(cache, spec):
  import numpy as np
  from jetlink.server.backends.trt import TrtBackend
  from jetlink.server.cache import EngineCache
  from jetlink.server.session import EngineHost, Request
  original = os.environ.get('CARROT_TRT_DEVICE_STATE')
  reference = []
  max_error = 0.
  try:
    for enabled in ('0', '1'):
      os.environ['CARROT_TRT_DEVICE_STATE'] = enabled
      host = EngineHost(EngineCache(Path(cache), TrtBackend()))
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
          values = loaded.engine.run()
          if not values or not all(np.isfinite(v).all() for v in values.values()):
            raise ValueError('State parity inference is not finite')
          output = values['outputs'].copy()
          if enabled == '0':
            reference.append(output)
          else:
            difference = float(np.max(np.abs(output.astype(np.float64) - reference[frame])))
            max_error = max(max_error, difference)
            np.testing.assert_allclose(output, reference[frame], rtol=1e-4, atol=1e-4,
                                       err_msg=f'GPU state feedback differs at frame {frame}')
          loaded.queues.feedback(values)
      finally:
        host.close()
    print('DEVICE_RECURRENCE_PARITY_OK', 32, 'frames; reset at 0,16; max_abs', max_error, flush=True)
  finally:
    if original is None:
      os.environ.pop('CARROT_TRT_DEVICE_STATE', None)
    else:
      os.environ['CARROT_TRT_DEVICE_STATE'] = original
