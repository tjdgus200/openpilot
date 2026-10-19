"""Sign a committed host bundle/model for the separately enrolled USB channel."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import tarfile

from usb_release import RUNTIME, canonical, validate, verify


def identity(path, url):
  sha = hashlib.sha256()
  with path.open('rb') as source:
    while data := source.read(1 << 20):
      sha.update(data)
  return {'sha256': sha.hexdigest(), 'size': path.stat().st_size, 'url': url}


def prepare_sha256(bundle, model, profile, bundle_url, model_url, output):
  """Publish plaintext metadata and expected hashes; no cryptographic keys."""
  with tarfile.open(bundle) as archive:
    commit = archive.extractfile('SOURCE_COMMIT').read().decode().strip()
    spec = json.load(archive.extractfile('openpilot/selfdrive/modeld/jetlink/jetson_model.json'))
    compatibility = json.load(archive.extractfile('tools/jetlink/runtime_compatibility.json'))
  if compatibility['protected_storage_format'] != 1:
    raise ValueError('Bundle lacks protected-storage compatibility')
  value = {'format': 2, 'delivery': 'usb', 'integrity': 'sha256', 'profile': profile, 'source_commit': commit,
           'runtime': RUNTIME, 'storage_format': 1, 'bundle': identity(bundle, bundle_url), 'model': identity(model, model_url)}
  validate(value)
  if (spec['sha256'], spec['nbytes']) != (value['model']['sha256'], value['model']['size']):
    raise ValueError('ONNX does not match bundled model pin')
  output.mkdir(parents=True, exist_ok=True)
  (output / 'usb_host_sha256_release.json').write_text(json.dumps(value, indent=2) + '\n')
  return value


def prepare(bundle, model, private_key, profile, bundle_url, model_url, output):
  from Crypto.PublicKey import ECC
  from Crypto.Signature import eddsa
  root = Path(__file__).resolve().parents[2]
  if private_key.resolve().is_relative_to(root):
    raise ValueError('Keep signing private keys outside the Git checkout')
  key = ECC.import_key(private_key.read_text())
  if not key.has_private() or key.curve != 'Ed25519':
    raise ValueError('Expected the owner experiment Ed25519 private key')
  with tarfile.open(bundle) as archive:
    commit = archive.extractfile('SOURCE_COMMIT').read().decode().strip()
    spec = json.load(archive.extractfile('openpilot/selfdrive/modeld/jetlink/jetson_model.json'))
    compatibility = json.load(archive.extractfile('tools/jetlink/runtime_compatibility.json'))
  if compatibility['protected_storage_format'] != 1:
    raise ValueError('Bundle lacks protected-storage compatibility')
  value = {'format': 1, 'delivery': 'usb', 'profile': profile, 'source_commit': commit,
           'runtime': RUNTIME, 'storage_format': 1, 'bundle': identity(bundle, bundle_url),
           'model': identity(model, model_url), 'signature': ''}
  if (spec['sha256'], spec['nbytes']) != (value['model']['sha256'], value['model']['size']):
    raise ValueError('ONNX does not match bundled model pin')
  validate(value)
  value['signature'] = base64.b64encode(eddsa.new(key, 'rfc8032').sign(canonical(value))).decode()
  output.mkdir(parents=True, exist_ok=True)
  public = output / 'usb_host_public.pem'
  if public.exists() and ECC.import_key(public.read_text()).export_key(format='DER') != key.public_key().export_key(format='DER'):
    raise ValueError('Refusing to replace another release public key')
  public.write_text(key.public_key().export_key(format='PEM') + '\n')
  verify(value, public)
  (output / 'usb_host_release.json').write_text(json.dumps(value, indent=2) + '\n')
  print('Signed USB selection:', commit, profile)
  print('Publish both assets at the signed URLs, then commit only the manifest and public key to the experiment branch.')


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  for name in ('bundle', 'model', 'output'):
    parser.add_argument('--' + name, type=Path, required=True)
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument('--private-key', type=Path)
  mode.add_argument('--sha256', action='store_true')
  for name in ('profile', 'bundle-url', 'model-url'):
    parser.add_argument('--' + name, required=True)
  args = parser.parse_args()
  if args.sha256:
    prepare_sha256(args.bundle, args.model, args.profile, args.bundle_url, args.model_url, args.output)
  else:
    prepare(args.bundle, args.model, args.private_key, args.profile, args.bundle_url, args.model_url, args.output)
