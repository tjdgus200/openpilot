"""Boot-latched, optional PV5 native driving model."""
import hashlib
import os
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from openpilot.common.big_model_catalog import atomic_json, boot_id, read_json, synchronized
from openpilot.system.manager.pv5_build_recovery import pv5_build_recovery_enabled


CD210 = 'cd210'
VEGA = 'polaris-vega-2'
MODELS = {CD210: 'CD210', VEGA: 'Polaris Vega 2'}
ROOT = Path('/data/media/0/carrot/pv5-internal-model')
BOOT_PATH = Path('/dev/shm/carrot-pv5-internal-model.json')
CP_PATH = Path('/data/params/d/CarParamsPersistent')
REPO = Path(__file__).resolve().parents[3]
ONNX_URL = ('https://huggingface.co/buckets/StarPilot-Driving/StarPilot-Resources/resolve/'
            'onnx/spz_polaris_vega2/spz_polaris_vega2_driving_supercombo.onnx')
ONNX_SIZE = 96_696_386
ONNX_SHA256 = 'a57171b0c122abe22572a3fc83910c4eadc054455caf84fc581c193597d92e73'


def supported_car(cp):
  return cp is not None and cp.brand == 'hyundai' and cp.carFingerprint == 'KIA_PV5' and not cp.notCar


def available():
  if not Path('/AGNOS').is_file():
    return False
  try:
    stamp = CP_PATH.stat()
    return _persisted_pv5(stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns)
  except OSError:
    return False


@lru_cache(maxsize=1)
def _persisted_pv5(_inode, _size, _mtime, _ctime):
  return pv5_build_recovery_enabled()


def _selected(value):
  return value if isinstance(value, str) and value in MODELS else CD210


def _preference():
  return _selected(read_json(ROOT / 'selection.json').get('selected'))


def _boot():
  state = read_json(BOOT_PATH)
  return state if state.get('boot') == boot_id() else {}


def status():
  if not available():
    return None
  boot, preference = _boot(), _preference()
  state = boot.get('state')
  state = 'preparing' if state in ('preparing', 'starting') else 'fallback' if state == 'fallback' else 'ready'
  return {'models': [{'id': key, 'name': name} for key, name in MODELS.items()],
          'selected': preference, 'effective': _selected(boot.get('effective')),
          'reboot_required': preference != _selected(boot.get('selected')),
          'state': state}


@synchronized
def select(key):
  if not isinstance(key, str) or key not in MODELS or not available():
    raise ValueError('Internal model selection is available only on a supported PV5')
  if not _boot():
    # The vehicle may have been identified for the first time after launch.
    _initialize_boot(CD210)
  atomic_json(ROOT / 'selection.json', {'selected': key})


def _initialize_boot(key):
  state = {'boot': boot_id(), 'selected': key, 'effective': CD210,
           'state': 'ready' if key == CD210 else 'preparing'}
  atomic_json(BOOT_PATH, state)
  return state


@synchronized
def _freeze_boot():
  if existing := _boot():
    if existing.get('state') == 'preparing':
      record_loaded(False)
    return None
  return _initialize_boot(_preference())


def _verify(path, size, expected):
  if type(size) is not int or size <= 0 or not isinstance(expected, str):
    return False
  try:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
      return False
    with path.open('rb') as stream:
      return hashlib.file_digest(stream, 'sha256').hexdigest() == expected
  except OSError:
    return False


def _download(path):
  if _verify(path, ONNX_SIZE, ONNX_SHA256):
    return
  deadline, count = time.monotonic() + 90, 0
  with tempfile.TemporaryDirectory(dir=ROOT) as directory:
    target = Path(directory) / path.name
    with urlopen(ONNX_URL, timeout=15) as response, target.open('wb') as out:
      if not response.geturl().startswith('https://'):
        raise ValueError('Insecure native model redirect')
      while chunk := response.read(1 << 20):
        count += len(chunk)
        if count > ONNX_SIZE or time.monotonic() > deadline:
          raise ValueError('Native model download limit exceeded')
        out.write(chunk)
      out.flush()
      os.fsync(out.fileno())
    if not _verify(target, ONNX_SIZE, ONNX_SHA256):
      raise ValueError('Native model checksum or length mismatch')
    os.replace(target, path)


def _compile(onnx, pkl):
  from openpilot.common.transformations.camera import _ar_ox_fisheye, _os_fisheye
  from openpilot.common.transformations.model import MEDMODEL_INPUT_SIZE
  from openpilot.selfdrive.modeld.constants import ModelConstants
  from openpilot.selfdrive.modeld.helpers import get_tg_input_devices

  devices = get_tg_input_devices('openpilot.selfdrive.modeld.modeld', False)
  if devices != {'WARP_DEV': 'QCOM', 'QUEUE_DEV': 'QCOM'}:
    raise ValueError('Unsupported native model compile backend')
  with tempfile.TemporaryDirectory(dir=ROOT) as directory:
    working = Path(directory)
    source, output = working / onnx.name, working / pkl.name
    # The generic compiler also handles split models. Feed it this exact hash-
    # checked graph without any neighboring manifest, and avoid copying 97 MB.
    os.link(onnx, source)
    model_w, model_h = MEDMODEL_INPUT_SIZE
    cameras = (f'{cam.width}x{cam.height}' for cam in (_ar_ox_fisheye, _os_fisheye))
    frame_skip = ModelConstants.MODEL_RUN_FREQ // ModelConstants.MODEL_CONTEXT_FREQ
    # Same QCOM flags, warp dimensions and history cadence as the stock SCons target.
    env = dict(os.environ, DEV='QCOM', WARP_DEV='QCOM', IMAGE='1', FLOAT16='1', NOLOCALS='1',
               JIT_BATCH_SIZE='0', OPENPILOT_HACKS='1')
    subprocess.run(['taskset', '-c', '7', sys.executable, str(Path(__file__).with_name('compile_modeld.py')),
                    '--model-size', f'{model_w}x{model_h}', '--camera-resolutions', *cameras,
                    '--onnx', str(source), '--output', str(output), '--frame-skip', str(frame_skip)],
                   cwd=REPO, env=env, check=True, timeout=360)
    with output.open('r+b') as stream:
      sha = hashlib.file_digest(stream, 'sha256').hexdigest()
      os.fsync(stream.fileno())
    size = output.stat().st_size
    if size <= 0:
      raise ValueError('Empty native model artifact')
    os.replace(output, pkl)
    return {'size': size, 'sha256': sha}


def _build_identity():
  from openpilot.selfdrive.modeld.precompiled_validation import device_context

  revision = subprocess.check_output(['git', 'rev-parse', 'HEAD:tinygrad_repo'], cwd=REPO, text=True, timeout=5).strip()
  if not re.fullmatch('[0-9a-f]{40}', revision):
    raise ValueError('Cannot identify the local tinygrad version')
  compiler = Path(__file__).parent
  files = [Path(__file__), *(compiler / name for name in ('compile_modeld.py', 'get_model_metadata.py', 'helpers.py', 'constants.py')),
           REPO / 'openpilot/common/file_chunker.py', REPO / 'openpilot/common/transformations/camera.py',
           REPO / 'openpilot/common/transformations/model.py', REPO / 'openpilot/system/camerad/cameras/nv12_info.py']
  context = device_context()
  context.pop('machine_id', None)
  return {'source': ONNX_SHA256, 'tinygrad': revision, 'device': context,
          'compiler': [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]}


def prepare_boot():
  """Runs before manager in the existing launcher lock, including prebuilt installs."""
  if not available():
    return
  state = _freeze_boot()
  if state is None or state['selected'] == CD210:
    return
  try:
    ROOT.mkdir(parents=True, exist_ok=True)
    identity = _build_identity()
    pkl, onnx, manifest = ROOT / 'vega2-qcom.pkl', ROOT / 'vega2.onnx', ROOT / 'compiled.json'
    marker = read_json(manifest)
    if marker.get('identity') != identity or not _verify(pkl, marker.get('size'), marker.get('sha256')):
      _download(onnx)
      marker = {'identity': identity, **_compile(onnx, pkl)}
      atomic_json(manifest, marker)
    state.update(state='ready', size=marker['size'], sha256=marker['sha256'])
  except Exception as exc:
    print(f'Polaris Vega 2 preparation failed ({type(exc).__name__}); using CD210', flush=True)
    state.update(state='fallback')
  atomic_json(BOOT_PATH, state)


def prepared_path():
  boot = _boot()
  path = ROOT / 'vega2-qcom.pkl'
  if boot.get('selected') == VEGA and boot.get('state') == 'starting':
    # The prior process exited without producing an initial valid result.
    record_loaded(False)
  elif boot.get('selected') == VEGA and boot.get('state') == 'ready':
    if _verify(path, boot.get('size'), boot.get('sha256')):
      return path
    record_loaded(False)
  return None


def starting():
  boot = _boot()
  if boot.get('selected') != VEGA or boot.get('state') != 'ready':
    raise ValueError('Native alternative is not prepared for this boot')
  boot.update(state='starting', effective=CD210)
  atomic_json(BOOT_PATH, boot)


def record_loaded(loaded):
  boot = _boot()
  if boot.get('selected') == VEGA:
    boot.update(effective=VEGA if loaded else CD210, state='ready' if loaded else 'fallback')
    atomic_json(BOOT_PATH, boot)


if __name__ == '__main__':
  prepare_boot()
