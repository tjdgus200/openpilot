"""Export committed host source and the explicitly supplied PV5 radar runtime."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
PATHS = ['LICENSE', 'opendbc_repo/LICENSE', 'openpilot/__init__.py', 'openpilot/common', 'openpilot/cereal',
         'openpilot/selfdrive/carrot/cluster', 'openpilot/selfdrive/carrot/carrot_navi.py',
         'openpilot/selfdrive/carrot/carrot_navi_cereal.py', 'openpilot/selfdrive/carrot/deceleration_source.py',
         'openpilot/selfdrive/carrot/signal_display.py', 'openpilot/selfdrive/carrot/signal_mode.py',
         'openpilot/selfdrive/carrot/lead_brake_lights.py', 'openpilot/selfdrive/carrot/obd_battery.py',
         'openpilot/selfdrive/carrot/obd_debug.py',
         'openpilot/selfdrive/navigation/preview.py',
         'openpilot/selfdrive/controls/lib', 'openpilot/selfdrive/modeld/constants.py',
         'openpilot/selfdrive/modeld/model_catalog', 'openpilot/selfdrive/modeld/jetlink/cinque_v2.json',
         'openpilot/selfdrive/modeld/jetlink/jetson_model.json', 'openpilot/selfdrive/assets',
         'openpilot/system/hardware', 'openpilot/system/version.py',
         'opendbc_repo/opendbc/car/car.capnp', 'opendbc_repo/opendbc/car/include',
         'third_party/jetlink', 'tools/jetlink', 'tools/pv5_radar_labels']


RADAR_RUNTIME_DIR = 'tools/pv5_radar_labels/runtime'
RADAR_ASSETS = {'radar-label-worker': 0o755, 'libpv5_radar_labels.so': 0o644,
                'yolov5n.onnx': 0o644, 'YOLOV5-LICENSE': 0o644}
YOLO_RELEASE = 'https://github.com/ultralytics/yolov5/releases/tag/v7.0'
YOLO_SOURCE = 'https://github.com/ultralytics/yolov5/tree/v7.0'
YOLO_CHECKPOINT = 'https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.pt'
YOLO_LICENSE_SHA256 = '7fa7e56c0db71105f4b59a6a0ac40225eb059876f85cd380383f7d7a066e68be'


def _arm_elf(data, allowed_types):
  return (len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01'
          and struct.unpack_from('<H', data, 16)[0] in allowed_types
          and struct.unpack_from('<H', data, 18)[0] == 183)


def radar_runtime_members(directory):
  """Validate the offline release inputs. The device creates its own TRT plan."""
  from tools.pv5_radar_labels.native.build_engine import MODEL_BYTES, MODEL_SHA256

  if not directory.is_dir() or directory.is_symlink():
    raise ValueError('Expected a native radar runtime directory')
  members = {}
  for name, mode in RADAR_ASSETS.items():
    path = directory / name
    if not path.is_file() or path.is_symlink():
      raise ValueError(f'Missing radar runtime asset: {name}')
    members[name] = (path.read_bytes(), mode)
  if not _arm_elf(members['radar-label-worker'][0], (2, 3)):
    raise ValueError('Radar worker must be AArch64 ELF')
  if not _arm_elf(members['libpv5_radar_labels.so'][0], (3,)):
    raise ValueError('Radar detector library must be AArch64 ELF')
  model = members['yolov5n.onnx'][0]
  if (hashlib.sha256(model).hexdigest(), len(model)) != (MODEL_SHA256, MODEL_BYTES):
    raise ValueError('Radar detector does not match the pinned Ultralytics v7.0 ONNX')
  if hashlib.sha256(members['YOLOV5-LICENSE'][0]).hexdigest() != YOLO_LICENSE_SHA256:
    raise ValueError('Expected the upstream YOLOv5 v7.0 license')

  notices = directory / 'notices'
  if not notices.is_dir() or notices.is_symlink():
    raise ValueError('Missing native build and license notices')
  for path in sorted(notices.rglob('*')):
    if path.is_symlink() or (path.is_file() and path.stat().st_size > 4 << 20):
      raise ValueError(f'Invalid native build notice: {path.name}')
    if path.is_file():
      members[path.relative_to(directory).as_posix()] = (path.read_bytes(), 0o644)
  if 'notices/provenance.json' not in members:
    raise ValueError('Missing native build provenance')
  provenance = json.loads(members['notices/provenance.json'][0])
  if provenance['target'] != 'aarch64-unknown-linux-gnu' or provenance['sm'] != 87:
    raise ValueError('Native provenance does not identify an Orin AArch64 build')
  for name in ('radar-label-worker', 'libpv5_radar_labels.so'):
    record = provenance['files'][name]
    data = members[name][0]
    if (record['sha256'], record['bytes']) != (hashlib.sha256(data).hexdigest(), len(data)):
      raise ValueError(f'Native build provenance mismatch: {name}')

  notice = (
    'This archive contains the original Ultralytics YOLOv5n v7.0 ONNX asset.\n'
    'The fixed v7.0 upstream source tree carries GNU GPL version 3 (YOLOV5-LICENSE).\n'
    f'Unmodified release and ONNX export: {YOLO_RELEASE}\n'
    f'Corresponding model source, configuration and export tools: {YOLO_SOURCE}\n'
    f'Original weight checkpoint: {YOLO_CHECKPOINT}\n'
    'The included detector build helper produces the device-local TensorRT plan.\n'
  )
  members['YOLOV5-SOURCE.txt'] = (notice.encode('utf-8'), 0o644)
  return members


def add_bytes(archive, name, data, mode=0o644):
  member = tarfile.TarInfo(name)
  member.size = len(data)
  member.mode = mode
  archive.addfile(member, io.BytesIO(data))


def export_radar_runtime(source, target, members):
  if members and any(f'{RADAR_RUNTIME_DIR}/{name}' in source.getnames() for name in members):
    raise ValueError('Committed host source duplicates a supplied radar runtime asset')
  if members:
    provenance = json.loads(members['notices/provenance.json'][0])
    native_source = Path(RADAR_RUNTIME_DIR).parent.as_posix()
    for name, sha256 in provenance['source_sha256'].items():
      record = source.extractfile(f'{native_source}/{name}')
      if record is None or hashlib.sha256(record.read()).hexdigest() != sha256:
        raise ValueError(f'Native source differs from build provenance: {name}')
  for name, (data, mode) in members.items():
    add_bytes(target, f'{RADAR_RUNTIME_DIR}/{name}', data, mode)


def is_pv5_catalog_spec(root, model_spec):
  if model_spec is None:
    return False
  catalog_dir = root / 'openpilot/selfdrive/modeld/model_catalog'
  if Path(model_spec).parent.as_posix() != 'openpilot/selfdrive/modeld/model_catalog':
    return False
  catalog = json.loads((catalog_dir / 'catalog.json').read_text(encoding='utf-8'))
  spec_name = Path(model_spec).name
  return any(entry.get('jetson_spec') == spec_name and (catalog_dir / f'pv5-usb-{model}.json').is_file()
             for model, entry in catalog.items())


def export_members(source, target, model_spec=None):
  # Installed bootstrap revisions read this legacy filename before running
  # any candidate code. Give them the selected pin too; the checkout keeps
  # the historical Cinque spec for Mac/phone compatibility and regression tests.
  legacy = 'openpilot/selfdrive/modeld/jetlink/cinque_v2.json'
  selected = 'openpilot/selfdrive/modeld/jetlink/jetson_model.json'
  if model_spec is not None:
    if (not model_spec.startswith('openpilot/selfdrive/modeld/model_catalog/')
        or '..' in model_spec.split('/') or not model_spec.endswith('.json')):
      raise ValueError('Model spec must be a committed catalog JSON')
    sys.path.insert(0, str(ROOT / 'third_party/jetlink'))
    from jetlink.spec import ModelSpec
    ModelSpec.from_dict(json.load(source.extractfile(model_spec)))
  for member in source:
    if (model_spec is not None and member.name in (legacy, selected)
        or member.name == legacy and selected in source.getnames()):
      data = source.extractfile(model_spec or selected).read()
      member.size = len(data)
      target.addfile(member, io.BytesIO(data))
    else:
      target.addfile(member, source.extractfile(member) if member.isfile() else None)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('output', type=Path)
  parser.add_argument('--model-spec', help='Committed catalog spec for this model-specific bundle')
  parser.add_argument('--radar-runtime-dir', type=Path, help='Verified native PV5 radar release assets')
  args = parser.parse_args()
  if args.radar_runtime_dir is None and is_pv5_catalog_spec(ROOT, args.model_spec):
    parser.error('--radar-runtime-dir is required for a PV5 model-paired host bundle')
  sys.path.insert(0, str(ROOT))
  runtime = radar_runtime_members(args.radar_runtime_dir) if args.radar_runtime_dir is not None else {}
  git = ['git', '-c', f'safe.directory={ROOT.as_posix()}', '-C', str(ROOT)]
  subprocess.run([*git, 'diff', '--quiet', 'HEAD', '--', *PATHS], check=True)
  untracked = subprocess.check_output([*git, 'ls-files', '--others', '--exclude-standard', '--', *PATHS])
  if untracked.strip():
    raise RuntimeError('Commit or remove untracked host source files before exporting')
  commit = subprocess.check_output([*git, 'rev-parse', 'HEAD'])
  # Inspect cleanliness using the checkout's own CRLF policy; only the Linux
  # export forces LF. Changing that policy for diff misclassifies Windows files.
  archive = subprocess.check_output([*git, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                                     'archive', 'HEAD', *PATHS])
  with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source, tarfile.open(args.output, 'w:gz') as target:
    export_members(source, target, args.model_spec)
    export_radar_runtime(source, target, runtime)
    add_bytes(target, 'SOURCE_COMMIT', commit)
  sha = hashlib.sha256()
  with args.output.open('rb') as bundle:
    for block in iter(lambda: bundle.read(4 << 20), b''):
      sha.update(block)
  args.output.with_suffix(args.output.suffix + '.sha256').write_text(
    f'{sha.hexdigest()}  {args.output.name}\n', encoding='utf-8', newline='\n')
  args.output.with_suffix(args.output.suffix + '.json').write_text(json.dumps({
    'source_commit': commit.decode().strip(), 'bundle': args.output.name,
    'size': args.output.stat().st_size, 'sha256': sha.hexdigest(),
  }, indent=2) + '\n', encoding='utf-8', newline='\n')
  print(commit.decode().strip(), args.output)


if __name__ == '__main__':
  main()
