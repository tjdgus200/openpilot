"""Separate signed trust domain for comma-delivered experiments (never NAS trust)."""
import base64
import hashlib
import json
import re
import struct
from urllib.parse import urlsplit

DELIVERY = 'comma-relay'
CAPABILITY = 'carrot_comma_relay_v1'
QUERY, CHUNK, FINISH, ACK = 0x4010, 0x4011, 0x4012, 0x4013
CANCEL = 0x4014
HEADER = struct.Struct('<32sBQ')  # manifest digest, asset number, byte offset
CHUNK_BYTES = 256 << 10
ASSETS = ('bundle', 'model')
RUNTIME = {'arch': 'aarch64', 'l4t': '36.4.7', 'tensorrt': '10.3.0'}


def canonical(value):
  return json.dumps({k: v for k, v in value.items() if k != 'signature'}, sort_keys=True, separators=(',', ':')).encode()


def identity(value):
  return hashlib.sha256(canonical(value)).hexdigest()


def checked_url(url, redirect=False):
  parsed = urlsplit(url)
  allowed = ((parsed.netloc == 'github.com' and parsed.path.startswith('/tjdgus200/openpilot/releases/download/')) or
             (parsed.netloc == 'upload.shind0.synology.me' and parsed.path.startswith('/models/')) or
             (redirect and parsed.netloc == 'release-assets.githubusercontent.com'))
  if parsed.scheme != 'https' or not allowed or parsed.fragment:
    raise ValueError('Unapproved comma relay asset URL')
  return url


def validate(value):
  if (not isinstance(value, dict) or set(value) != {'format', 'delivery', 'source_commit', 'runtime', 'storage_format',
                                                 'bundle', 'model', 'signature'} or
      type(value['format']) is not int or value['format'] != 1 or value['delivery'] != DELIVERY or
      value['runtime'] != RUNTIME or type(value['storage_format']) is not int or value['storage_format'] != 1 or
      re.fullmatch('[0-9a-f]{40}', str(value['source_commit'])) is None):
    raise ValueError('Invalid comma relay manifest or runtime ABI')
  for name, limit in (('bundle', 256 << 20), ('model', 2 << 30)):
    item = value[name]
    if (not isinstance(item, dict) or set(item) != {'sha256', 'size', 'url'} or
        re.fullmatch('[0-9a-f]{64}', str(item['sha256'])) is None or
        type(item['size']) is not int or not 0 < item['size'] <= limit):
      raise ValueError('Invalid relay asset identity')
    checked_url(item['url'])
  if not isinstance(value['signature'], str) or len(base64.b64decode(value['signature'], validate=True)) != 64:
    raise ValueError('Invalid relay signature encoding')
  return value


def verify(value, public_key):
  from Crypto.PublicKey import ECC
  from Crypto.Signature import eddsa
  validate(value)
  key = ECC.import_key(public_key)
  if key.has_private() or key.curve != 'Ed25519':
    raise ValueError('Expected a public Ed25519 relay key')
  eddsa.new(key, 'rfc8032').verify(canonical(value), base64.b64decode(value['signature'], validate=True))
  return value
