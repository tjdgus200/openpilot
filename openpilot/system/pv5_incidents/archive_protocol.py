"""Bounded owner-PC archive protocol. No subscribers, workers or file I/O."""

import hashlib
import hmac
import json
import re
import secrets
import time

VERSION = 1
PRIVACY = 'no-audio-cabin-v1'
CHUNK = 65536
MAX_MANIFEST = 8192
MAX_FILE = 512 * 1024 * 1024
MAX_FILES = 8
NAMES = frozenset(('rlog.zst', 'rlog.bz2', 'rlog', 'fcamera.hevc', 'fcamera.ts', 'ecamera.hevc', 'ecamera.ts', 'qcamera.ts', 'qcamera.mp4'))
RLOG_NAMES = ('rlog.zst', 'rlog.bz2', 'rlog')
ZERO_CHAIN = '0' * 64
TOKEN = re.compile(r'^[0-9a-f]{64}$')
SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.|\-]{0,126}--[0-9]{1,6}$')


def encode(value):
  return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def identifier(key, kind, value):
  return hmac.new(key, b'pv5-archive-id-v1\0' + kind.encode() + b'\0' + encode(value), hashlib.sha256).hexdigest()


def file_id(key, segment, name, size, mtime_ns):
  return identifier(key, 'file', [PRIVACY, segment, name, size, mtime_ns])


def advance(chain, body):
  if not TOKEN.fullmatch(chain) or not 0 < len(body) <= CHUNK:
    raise ValueError('invalid_chunk_chain')
  return hashlib.sha256(bytes.fromhex(chain) + hashlib.sha256(body).digest()).hexdigest()


def sign(key, stamp, nonce, path, body):
  text = f'pv5-archive-v1\n{stamp}\n{nonce}\n{path}\n'.encode() + hashlib.sha256(body).digest()
  return hmac.new(key, text, hashlib.sha256).hexdigest()


def headers(key, path, body, now=None, nonce=None):
  stamp = int(time.time() if now is None else now)
  nonce = nonce or secrets.token_hex(16)
  return {'X-PV5-Time': str(stamp), 'X-PV5-Nonce': nonce, 'X-PV5-Signature': sign(key, stamp, nonce, path, body), 'Content-Type': 'application/octet-stream'}


def authenticate(key, path, values, body, now):
  stamp, nonce, digest = (values.get(name, '') for name in ('X-PV5-Time', 'X-PV5-Nonce', 'X-PV5-Signature'))
  return (
    bool(re.fullmatch(r'[0-9]{1,10}', stamp))
    and bool(re.fullmatch(r'[0-9a-f]{32}', nonce))
    and bool(TOKEN.fullmatch(digest))
    and abs(now - int(stamp)) <= 300
    and hmac.compare_digest(digest, sign(key, int(stamp), nonce, path, body))
  )


def manifest(key, value):
  if not isinstance(value, dict) or set(value) != {'version', 'privacy', 'segment', 'files', 'uploader_commit'}:
    raise ValueError('invalid_manifest')
  if value['privacy'] != PRIVACY:
    raise ValueError('privacy_filter_required')
  if value['version'] != VERSION or type(value['version']) is not int:
    raise ValueError('invalid_version')
  segment, files, commit = value['segment'], value['files'], value['uploader_commit']
  if not isinstance(segment, str) or not SEGMENT.fullmatch(segment) or '..' in segment:
    raise ValueError('invalid_segment')
  if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|unknown', commit):
    raise ValueError('invalid_commit')
  if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
    raise ValueError('invalid_files')
  names = set()
  for entry in files:
    if not isinstance(entry, dict) or set(entry) != {'name', 'size', 'mtime_ns', 'file_id'}:
      raise ValueError('invalid_file')
    name, size, mtime = entry['name'], entry['size'], entry['mtime_ns']
    if name not in NAMES or name in names:
      raise ValueError('invalid_filename')
    if type(size) is not int or not 0 < size <= MAX_FILE or type(mtime) is not int or not 0 < mtime < 2**63:
      raise ValueError('invalid_file_size')
    if entry['file_id'] != file_id(key, segment, name, size, mtime):
      raise ValueError('invalid_file_id')
    names.add(name)
  if len(names.intersection(RLOG_NAMES)) > 1:
    raise ValueError('duplicate_rlog_sources')
  if len(encode(value)) > MAX_MANIFEST:
    raise ValueError('manifest_too_large')
  return identifier(key, 'segment', value), value
