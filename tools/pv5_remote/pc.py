"""PC-only authenticated public-key bootstrap. Never exposes private keys."""

from collections import OrderedDict
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from openpilot.system.pv5_incidents.archive_protocol import authenticate, encode
from tools.pv5_remote.conditional_remote import HOME_POLICY

PATH = '/v1/remote/bootstrap'
QUALIFICATION = '/v1/remote/qualification'


def public_key(value):
  if not isinstance(value, str) or len(value) > 256:
    raise ValueError('invalid_public_key')
  match = re.fullmatch(r'ssh-ed25519 ([A-Za-z0-9+/]{68})', value)
  if not match:
    raise ValueError('invalid_public_key')
  raw = base64.b64decode(match[1], validate=True)
  if len(raw) != 51 or raw[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
    raise ValueError('invalid_public_key')
  return value


class Bootstrap:
  def __init__(self, folder, key, enabled=lambda: False):
    self.folder, self.key, self.enabled = Path(folder), key, enabled
    self.nonces, self.lock = OrderedDict(), threading.Lock()
    if not self.folder.is_dir() or self.folder.is_symlink():
      raise ValueError('private_remote_directory_required')

  def ingest(self, path, values, body, now):
    if self.enabled() is not True or path not in (PATH, QUALIFICATION) or len(body) > 2048 or not authenticate(self.key, path, values, body, now):
      raise PermissionError('unauthorized')
    value = json.loads(body)
    if not isinstance(value, dict) or set(value) != {'version', 'host_key'} or type(value['version']) is not int or value['version'] != 1:
      raise ValueError('invalid_bootstrap')
    host = public_key(value['host_key'])
    with self.lock:
      while self.nonces and (len(self.nonces) >= 512 or next(iter(self.nonces.values())) < now - 300):
        self.nonces.popitem(last=False)
      nonce = values['X-PV5-Nonce']
      if nonce in self.nonces:
        raise PermissionError('replay')
      self.nonces[nonce] = now
      client_path, host_path = self.folder / 'client_ed25519.pub', self.folder / 'host_key.pub'
      if client_path.is_symlink() or host_path.is_symlink() or client_path.stat().st_size > 512:
        raise ValueError('invalid_key_storage')
      client = public_key(' '.join(client_path.read_text().split()[:2]))
      if host_path.exists():
        if host_path.stat().st_size > 256 or host_path.read_text().strip() != host:
          raise ValueError('host_key_changed')  # Never silently accept a replacement identity.
      else:
        if path == QUALIFICATION:
          raise ValueError('host_not_pinned')
        with host_path.open('x', encoding='ascii') as stream:
          stream.write(host + '\n')
          stream.flush()
          os.fsync(stream.fileno())
      known = self.folder / 'known_hosts'
      if known.is_symlink():
        raise ValueError('invalid_key_storage')
      text = 'pv5-conditional-owner ' + host + '\n'
      if known.exists() and (known.stat().st_size > 512 or known.read_text() != text):
        raise ValueError('host_key_changed')
      if not known.exists():
        with known.open('x', encoding='ascii') as stream:
          stream.write(text)
      if path == QUALIFICATION:
        proof = self.folder / 'home-qualification.json'
        if proof.is_symlink() or not proof.exists() or proof.stat().st_size > 8192:
          raise ValueError('qualification_required')
        result = json.loads(proof.read_bytes())
        if (
          result.get('passed') is not True or result.get('host_key') != host
          or result.get('resource_policy') != HOME_POLICY or not 0 <= now - result['stamp'] < 1800
        ):
          raise ValueError('qualification_required')
        return {
          'version': 1, 'home_qualified': True, 'guard_sha256': result['guard_sha256'],
          'resource_policy': HOME_POLICY, 'actual_device_key_auth': True, 'local_pid_audit_required': True,
        }
      return {'version': 1, 'client_public_key': client, 'host_key_pinned': True, 'host_fingerprint': hashlib.sha256(host.encode()).hexdigest()}


def handler(base, bootstrap):
  class Handler(base):
    def do_POST(self):
      if self.path not in (PATH, QUALIFICATION):
        return super().do_POST()
      length = self.headers.get('Content-Length', '')
      if self.headers.get('Transfer-Encoding') or not re.fullmatch(r'[0-9]{1,4}', length) or not 0 < int(length) <= 2048:
        self.send_error(413)
        return
      import time

      try:
        body = self.rfile.read(int(length))
        if len(body) != int(length):
          raise ValueError('truncated_bootstrap')
        value, status = bootstrap.ingest(self.path, self.headers, body, time.time()), 200
      except PermissionError:
        value, status = {'error': 'unauthorized'}, 403
      except (ValueError, OSError, KeyError, TypeError, RecursionError):
        value, status = {'error': 'bootstrap_refused'}, 422
      data = encode(value)
      self.send_response(status)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(data)))
      self.end_headers()
      self.wfile.write(data)

  return Handler
