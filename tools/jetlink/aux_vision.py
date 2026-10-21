"""Negotiate CPU vision once per USB session; image/result bytes are WLAN-only."""
import importlib.util
import json
import logging
from pathlib import Path
import secrets

from hud_wifi import Worker
from openpilot.selfdrive.carrot.xiaoge import remote as R

CAPABILITY = 'carrot_aux_cpu_wifi_v1'
DIAGNOSTICS_CAPABILITY = R.DIAGNOSTICS_CAPABILITY
REQUEST, RESPONSE = 0x4030, 0x4031
PORT = 7717
ROOT = Path(__file__).resolve().parents[2]


def support_problem(root=ROOT):
  try:
    if not Path('/etc/nv_tegra_release').is_file():
      return 'host_unsupported'
    if importlib.util.find_spec('cv2') is None:
      return 'dependency_missing'
    if not all((root / 'openpilot/selfdrive/carrot/xiaoge/assets' / name).is_file()
               for name in ('lane.onnx', 'v_asm_model.onnx')):
      return 'model_missing'
    return ''
  except (ImportError, OSError, ValueError):
    return 'runtime_incomplete'


def supported(root=ROOT):
  return not support_problem(root)


def end_session():
  R.write_session('disconnected')


def receiver(key):
  return Worker('receive', key, script='vision_cpu.py', port=PORT)


def publisher(client, peer):
  if peer.get('carrot_host') != 'jetson':
    R.write_session('host_unsupported', peer)
    return None
  if peer.get(CAPABILITY) is not True:
    code = 'runtime_outdated' if CAPABILITY not in peer else 'runtime_incomplete'
    if peer.get('carrot_aux_cpu_error') in ('dependency_missing', 'model_missing'):
      code = peer['carrot_aux_cpu_error']
    R.write_session(code, peer)
    return None
  worker = None
  R.write_session('negotiating', peer)
  try:
    key = secrets.token_hex(32)
    seq = client._next_seq()
    client.t.send_json(REQUEST, seq, {'key': key})
    reply = json.loads(bytes(client._expect(RESPONSE, seq, 3.).payload))
    if reply != {'ready': True}:
      R.write_session('session_setup_rejected', peer)
      return None
    try:
      worker = Worker('send', key, script='vision_cpu.py', port=PORT)
    except Exception:
      R.write_session('relay_start_failed', peer)
      return None
    R.write_session('session_ready', peer)
    return worker
  except Exception as error:
    if worker is not None:
      worker.close()
    from jetlink.transport.base import LinkTimeout
    R.write_session('session_setup_timeout' if isinstance(error, (TimeoutError, LinkTimeout)) else 'session_setup_failed', peer)
    logging.getLogger(__name__).warning('Auxiliary Wi-Fi unavailable; driving USB unchanged')
    return None
