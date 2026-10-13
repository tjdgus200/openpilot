"""Model pin shared by the comma client and Jetson release tooling."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = Path('openpilot/selfdrive/modeld/jetlink')


def read_spec(root=ROOT):
  directory = Path(root) / MODEL_DIR
  pin = directory / 'jetson_model.json'
  # Older signed bundles predate the separate Jetson pin.
  return json.loads((pin if pin.is_file() else directory / 'cinque_v2.json').read_text())


def is_jetson(peer):
  return isinstance(peer, dict) and (peer.get('carrot_host') == 'jetson' or
                                   (peer.get('backend') == 'trt' and str(peer.get('device', '')).startswith('Orin')))


def matching_release(path, required=False):
  """Never advertise a different model's signed release for this client.

  This checks model compatibility only; the Jetson still verifies the signature
  and download trust policy. Nothing here grants trust to an unsigned manifest.
  """
  try:
    manifest = json.loads(Path(path).read_text())
    model, spec = manifest['model'], read_spec()
    if model['sha256'] == spec['sha256'] and model['size'] == spec['nbytes']:
      return manifest
  except (OSError, ValueError, KeyError, TypeError):
    pass
  if required:
    raise ValueError('Selected Jetson model requires a matching signed host_release.json; release deployment is not configured')
  return None


def selected_release(path, required=False):
  relay = Path(path).with_name('comma_host_release.json')
  if relay.is_file():
    from tools.jetlink.relay_manifest import verify  # noqa: TID251 -- shared standalone boot-updater protocol
    value = verify(json.loads(relay.read_text()), relay.with_name('comma-relay-public.pem').read_text())
    spec = read_spec()
    if (value['model']['sha256'], value['model']['size']) != (spec['sha256'], spec['nbytes']):
      raise ValueError('Comma relay release does not match this branch model')
    return value
  local = Path(path).with_name('local_host_release.json')
  if not local.is_file():
    return matching_release(path, required)
  value, spec = json.loads(local.read_text()), read_spec()
  if (not isinstance(value, dict) or set(value) != {'format', 'delivery', 'profile', 'model'} or
      value['format'] != 1 or value['delivery'] != 'local' or
      re.fullmatch('[a-z][a-z0-9-]{0,63}', str(value['profile'])) is None or
      value['model'] != {'sha256': spec['sha256'], 'size': spec['nbytes']}):
    raise ValueError('Local Jetson selection does not match this branch model')
  return value
