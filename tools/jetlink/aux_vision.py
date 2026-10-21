"""Negotiate CPU vision once per USB session; image/result bytes are WLAN-only."""
import importlib.util
import json
import logging
from pathlib import Path
import secrets

from hud_wifi import Worker

CAPABILITY = 'carrot_aux_cpu_wifi_v1'
REQUEST, RESPONSE = 0x4030, 0x4031
PORT = 7717
ROOT = Path(__file__).resolve().parents[2]


def supported(root=ROOT):
  try:
    return (Path('/etc/nv_tegra_release').is_file() and importlib.util.find_spec('cv2') is not None
            and all((root / 'openpilot/selfdrive/carrot/xiaoge/assets' / name).is_file()
                    for name in ('lane.onnx', 'v_asm_model.onnx')))
  except (ImportError, OSError, ValueError):
    return False


def receiver(key):
  return Worker('receive', key, script='vision_cpu.py', port=PORT)


def publisher(client, peer):
  if peer.get('carrot_host') != 'jetson' or peer.get(CAPABILITY) is not True:
    return None
  worker = None
  try:
    key = secrets.token_hex(32)
    seq = client._next_seq()
    client.t.send_json(REQUEST, seq, {'key': key})
    reply = json.loads(bytes(client._expect(RESPONSE, seq, 3.).payload))
    if reply != {'ready': True}:
      return None
    worker = Worker('send', key, script='vision_cpu.py', port=PORT)
    return worker
  except Exception:
    if worker is not None:
      worker.close()
    logging.getLogger(__name__).warning('Auxiliary Wi-Fi unavailable; driving USB unchanged')
    return None
