"""PV5 GPU-state choice; USB transmission uses the official serial transport."""
import json
import os
from pathlib import Path

OPTIONS = Path(__file__).with_name('runtime_port_options.json')
KEYS = {'CARROT_TRT_DEVICE_STATE'}


def apply(path=OPTIONS, environ=None):
  env = os.environ if environ is None else environ
  if not path.exists():
    return {}
  value = json.loads(path.read_text())
  if not isinstance(value, dict) or set(value) != KEYS or any(type(v) is not bool for v in value.values()):
    raise ValueError('Invalid release runtime options')
  for key, enabled in value.items():
    # An explicit process option is for a separate controlled experiment.
    env.setdefault(key, str(int(enabled)))
  return {key: env[key] for key in KEYS}
