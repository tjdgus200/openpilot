"""Sign a committed host/ONNX pair on the PC; private key stays off both devices."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from install_comma_relay import HELPERS, inspect
from prepare_local_experiment import identity
from relay_manifest import DELIVERY, RUNTIME, canonical, verify


def prepare(bundle, model, key_path, output, bundle_url, model_url):
  if output.exists():
    raise FileExistsError('Use a fresh output directory')
  key = ECC.import_key(key_path.read_text())
  if key.curve != 'Ed25519' or not key.has_private():
    raise ValueError('Private Ed25519 signing key required on the PC')
  model_id = identity(model)
  with tarfile.open(bundle) as archive:
    source = archive.extractfile('SOURCE_COMMIT').read().decode().strip()
    spec = json.load(archive.extractfile('openpilot/selfdrive/modeld/jetlink/jetson_model.json'))
    if model_id != {'sha256': spec['sha256'], 'size': spec['nbytes']}:
      raise ValueError('Selected ONNX does not match the committed host pin')
    output.mkdir(parents=True)
    for name in (*HELPERS, 'install_comma_relay.py', 'install_local_experiment.py', 'release-signing-public.pem'):
      (output / name).write_bytes(archive.extractfile('tools/jetlink/' + name).read())
  value = {'format': 1, 'delivery': DELIVERY, 'source_commit': source, 'runtime': RUNTIME, 'storage_format': 1,
           'bundle': {**identity(bundle), 'url': bundle_url}, 'model': {**model_id, 'url': model_url}}
  value['signature'] = base64.b64encode(eddsa.new(key, 'rfc8032').sign(canonical(value))).decode()
  public = (key.public_key().export_key(format='PEM') + '\n').encode()
  verify(value, public)
  (output / 'comma-relay-public.pem').write_bytes(public)
  (output / 'comma_host_release.json').write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8', newline='\n')
  shutil.copyfile(bundle, output / 'host.tar.gz')
  fingerprint = hashlib.sha256(public).hexdigest()
  inspect(output, fingerprint)
  (output / 'INSTALL.txt').write_text(
    'One-time PC recovery/offline provisioning (DATA/runtime remains mounted):\n' +
    f'python3 install_comma_relay.py --runtime /MOUNTED/DATA/runtime --key-sha256 {fingerprint}\n' +
    'Replace the example runtime path with the verified DATA runtime mount. Never use an APP partition path.\n' +
    'Power off both devices before a branch change; reconnect after both branches are installed.\n' +
    'This package provisions the receiver only; comma downloads and transfers the pinned host/model.\n',
    encoding='utf-8', newline='\n')
  print('RELAY_PACKAGE_VERIFIED', source, fingerprint)


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--bundle', type=Path, required=True)
  parser.add_argument('--model', type=Path, required=True)
  parser.add_argument('--key', type=Path, required=True, help='Private key outside the Git checkout; never uploaded')
  parser.add_argument('--generate-key', action='store_true', help='Generate only if the key does not already exist')
  parser.add_argument('--output', type=Path, required=True)
  parser.add_argument('--bundle-url', required=True)
  parser.add_argument('--model-url', required=True)
  args = parser.parse_args()
  if args.generate_key:
    args.key.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
      stream.write(ECC.generate(curve='Ed25519').export_key(format='PEM') + '\n')
  prepare(args.bundle, args.model, args.key, args.output, args.bundle_url, args.model_url)
