"""PV5 release choices; keep untested hardware on the official serial writer."""
import json
import os
from pathlib import Path
import platform

OPTIONS = Path(__file__).with_name('runtime_port_options.json')
KEYS = {'CARROT_TRT_DEVICE_STATE', 'CARROT_JETLINK_AIO_WRITE'}


def c3_aio_supported():
  try:
    return (platform.system() == 'Linux' and platform.machine() == 'aarch64'
            and platform.release() == '4.9.103'
            and Path('/proc/device-tree/model').read_bytes().rstrip(b'\0\n') == b'comma tici')
  except OSError:
    return False


def apply(path=OPTIONS, environ=None):
  env = os.environ if environ is None else environ
  if not path.exists():
    return {}
  value = json.loads(path.read_text())
  if not isinstance(value, dict) or set(value) != KEYS or any(type(v) is not bool for v in value.values()):
    raise ValueError('Invalid release runtime options')
  for key, enabled in value.items():
    if key == 'CARROT_JETLINK_AIO_WRITE':
      enabled = enabled and c3_aio_supported()
    # An explicit process option is for a separate controlled experiment.
    env.setdefault(key, str(int(enabled)))
  return {key: env[key] for key in KEYS}
