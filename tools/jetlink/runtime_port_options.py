"""Release-scoped experiment options; official releases without this file stay off."""
import json
import os
from pathlib import Path

OPTIONS = Path(__file__).with_name('runtime_port_options.json')
KEYS = {'CARROT_TRT_DEVICE_STATE', 'CARROT_JETLINK_AIO_WRITE'}


def apply(path=OPTIONS, environ=None):
  env = os.environ if environ is None else environ
  if not path.exists():
    return {}
  value = json.loads(path.read_text())
  if not isinstance(value, dict) or set(value) != KEYS or any(type(v) is not bool for v in value.values()):
    raise ValueError('Invalid release experiment options')
  for key, enabled in value.items():
    env.setdefault(key, str(int(enabled)))
  return {key: env[key] for key in KEYS}
