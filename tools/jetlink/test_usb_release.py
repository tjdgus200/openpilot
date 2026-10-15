"""Real signatures/files and simulated USB; never touch installed services/devices."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace as NS

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1]), str(HERE.parents[1] / 'third_party/jetlink')]
import boot_update as boot
import install_usb_receiver as enroll
import prepare_usb_release as prepare
import update_host as update
import usb_release as usb
from jetlink import protocol as p
from jetlink.transport.base import Message
from openpilot.common import jetson_model
from openpilot.selfdrive.modeld.jetlink import startup
from tools.jetlink import usb_release_client as sender  # noqa: TID251 -- standalone updater package


def sign(value, key):
  value['signature'] = base64.b64encode(eddsa.new(key, 'rfc8032').sign(usb.canonical(value))).decode()
  return value


def make_bundle(path, commit, model):
  spec = {'sha256': hashlib.sha256(model).hexdigest(), 'nbytes': len(model)}
  files = {'SOURCE_COMMIT': commit.encode(),
           'openpilot/selfdrive/modeld/jetlink/jetson_model.json': json.dumps(spec).encode(),
           'tools/jetlink/runtime_compatibility.json': b'{"protected_storage_format":1}'}
  with tarfile.open(path, 'w:gz') as archive:
    for name, data in files.items():
      member = tarfile.TarInfo(name)
      member.size, member.mode = len(data), 0o644
      archive.addfile(member, io.BytesIO(data))


@pytest.fixture
def env(tmp_path, monkeypatch):
  if getattr(os, 'geteuid', lambda: -1)() != 0:
    pytest.skip('Linux root-owned catalog semantics')
  root = tmp_path / 'runtime'
  updater = root / 'updater'
  updater.mkdir(parents=True)
  for name in ('boot-update-required', 'protected-runtime.json'):
    (root / name).write_text('next-boot')
  for name in ('release-signing-public.pem', *enroll.FILES):
    (updater / name).write_bytes((HERE / name).read_bytes())
  previous = root / 'releases' / ('a' * 40)
  previous.mkdir(parents=True)
  (previous / 'SOURCE_COMMIT').write_text('a' * 40)
  (root / 'current').symlink_to(previous)
  (root / 'cache').mkdir()
  (root / 'cache/last-loaded.json').write_text('official-model')
  key = ECC.generate(curve='Ed25519')
  public = tmp_path / 'public.pem'
  public.write_text(key.public_key().export_key(format='PEM'))
  enroll.install(root, public)
  model = bytes(range(256)) * 2100  # multiple chunks and non-aligned final chunk
  bundle = tmp_path / 'host.tar.gz'
  make_bundle(bundle, 'b' * 40, model)
  model_path = tmp_path / 'model.onnx'
  model_path.write_bytes(model)
  value = sign({'format': 1, 'delivery': 'usb', 'profile': 'test-usb-1', 'runtime': usb.RUNTIME,
                'source_commit': 'b' * 40, 'storage_format': 1,
                'bundle': prepare.identity(bundle, 'https://example.invalid/host.tar.gz'),
                'model': prepare.identity(model_path, 'https://example.invalid/model.onnx')}, key)
  monkeypatch.setattr(update, 'ROOT', root)
  monkeypatch.setattr(update, 'PROTECTED_MARKER', tmp_path / 'protected')
  monkeypatch.setattr(update, 'check_runtime', lambda _: None)
  monkeypatch.setattr(update, 'probe_release', lambda _: None)
  monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: NS(returncode=3))
  import pwd
  monkeypatch.setattr(pwd, 'getpwnam', lambda _: NS(pw_uid=0, pw_gid=0))
  monkeypatch.setattr(boot, 'BOOT_ID', tmp_path / 'boot-id')
  monkeypatch.setattr(boot, 'READY', tmp_path / 'ready')
  boot.BOOT_ID.write_text('boot-1')
  monkeypatch.setattr(update.os, 'sync', lambda: None)
  return NS(root=root, value=value, key=key, public=public, bundle=bundle, model=model_path, previous=previous, tmp=tmp_path)


def transfer(env):
  receiver = usb.Receiver(env.root)
  assert receiver.begin(env.value) == {'complete': False}
  for i, path in enumerate((env.bundle, env.model)):
    data = path.read_bytes()
    for offset in range(0, len(data), usb.BLOCK):
      chunk = data[offset:offset + usb.BLOCK]
      assert receiver.chunk(usb.HEADER.pack(i, offset) + chunk) == {'index': i, 'offset': offset + len(chunk)}
  assert receiver.finish() == {'complete': True}
  receiver.close()


def activate(value):
  gate = boot.Gate()
  gate.select(value)
  gate.tick()
  if gate.worker is not None:
    gate.worker.join(5)
    assert not gate.worker.is_alive()
  gate.tick()
  return gate


def test_usb_registration_activation_and_official_restore(env, monkeypatch):
  original_key = (env.root / 'updater/release-signing-public.pem').read_bytes()
  transfer(env)
  assert (env.root / 'current').resolve() == env.previous
  assert not (env.root / 'updates/pending.json').exists()
  monkeypatch.setattr(update.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Jetson USB release attempted download'))
  assert activate(env.value).done
  assert (env.root / 'current/SOURCE_COMMIT').read_text() == 'b' * 40
  model = b'official return model'
  bundle = env.tmp / 'official.tar.gz'
  make_bundle(bundle, 'c' * 40, model)
  official = {'format': 1, 'source_commit': 'c' * 40, 'runtime': usb.RUNTIME, 'storage_format': 1,
              'bundle': prepare.identity(bundle, 'https://upload.shind0.synology.me/models/test/host.tar.gz'),
              'model': {'sha256': hashlib.sha256(model).hexdigest(), 'size': len(model),
                        'url': 'https://upload.shind0.synology.me/models/test/model.onnx'}}
  checked, downloaded = [], []
  monkeypatch.setattr(update, 'verify_signature', lambda v: checked.append(v))
  class Response(io.BytesIO):
    def __init__(self, url):
      self.url = url
      downloaded.append(url)
      super().__init__(bundle.read_bytes() if url.endswith('host.tar.gz') else model)
  monkeypatch.setattr(update.urllib.request, 'urlopen', lambda url, **k: Response(url))
  boot.BOOT_ID.write_text('boot-2')
  assert activate(official).done
  assert (env.root / 'current/SOURCE_COMMIT').read_text() == 'c' * 40
  assert len(downloaded) == 2 and checked and all(v == official for v in checked)
  assert (env.root / 'updater/release-signing-public.pem').read_bytes() == original_key
  boot.BOOT_ID.write_text('boot-3')
  assert activate(env.value).done  # offline registered experiment reuse
  assert len(downloaded) == 2


@pytest.mark.parametrize('change', ['signature', 'key', 'url', 'size', 'profile', 'runtime', 'source', 'missing-key'])
def test_untrusted_manifest_never_allocates_or_registers(env, change):
  value = copy.deepcopy(env.value)
  if change == 'key':
    sign(value, ECC.generate(curve='Ed25519'))
  elif change == 'signature':
    value['signature'] = ''
  elif change == 'missing-key':
    (env.root / 'updater/usb-experiment-public.pem').unlink()
  elif change == 'url':
    value['model']['url'] = 'https://different.invalid/file'
  elif change == 'size':
    value['model']['size'] += 1
  elif change == 'runtime':
    value['runtime']['l4t'] = '99'
  elif change == 'source':
    value['source_commit'] = 'd' * 40
  else:
    value['profile'] = '../outside'
  with pytest.raises((ValueError, FileNotFoundError)):
    usb.Receiver(env.root).begin(value)
  assert not (env.root / 'usb-incoming').exists()
  assert not (env.root / 'local-releases').exists()


@pytest.mark.parametrize('payload', [b'', usb.HEADER.pack(2, 0) + b'x', usb.HEADER.pack(0, 5) + b'x',
                                    usb.HEADER.pack(0, 0) + bytes(usb.BLOCK + 1)])
def test_bad_chunks_never_advance(env, payload):
  receiver = usb.Receiver(env.root)
  receiver.begin(env.value)
  with pytest.raises(ValueError):
    receiver.chunk(payload)
  assert receiver.offsets == [0, 0]
  receiver.close()


def test_disconnect_and_power_loss_restart_never_activate_partial_files(env):
  receiver = usb.Receiver(env.root)
  receiver.begin(env.value)
  receiver.chunk(usb.HEADER.pack(1, 0) + b'bad')
  abandoned = receiver.directory
  # Simulate process loss without close; a new session removes its staging only.
  replacement = usb.Receiver(env.root)
  replacement.begin(env.value)
  assert not abandoned.exists()
  assert replacement.offsets == [0, 0]
  with pytest.raises(ValueError, match='checksum/length'):
    replacement.finish()
  replacement.close()
  assert not (env.root / 'local-releases').exists()
  assert (env.root / 'current').resolve() == env.previous


def test_corruption_and_profile_collision_rejected(env):
  transfer(env)
  receiver = usb.Receiver(env.root)
  assert receiver.begin(env.value)['complete']
  changed = copy.deepcopy(env.value)
  changed['source_commit'] = 'c' * 40
  sign(changed, env.key)
  with pytest.raises(ValueError, match='another release'):
    receiver.begin(changed)
  (env.root / 'local-releases/test-usb-1/model.onnx').write_bytes(b'corrupt')
  with pytest.raises(ValueError, match='corrupt'):
    receiver.begin(env.value)


def test_failed_probe_preserves_official_files_and_withholds_ready(env, monkeypatch):
  transfer(env)
  def fail(_):
    (env.root / 'cache/last-loaded.json').write_text('bad-candidate')
    raise RuntimeError('synthetic inference failed')
  monkeypatch.setattr(update, 'probe_release', fail)
  gate = activate(env.value)
  assert gate.state == 'failed' and not gate.done
  assert (env.root / 'current').resolve() == env.previous
  assert (env.root / 'cache/last-loaded.json').read_text() == 'official-model'


class Wire:
  def __init__(self):
    self.responses, self.types = [], []
    self.session = boot.BootstrapSession(NS(send_json=self.reply), boot.Gate())
    self.client = NS(t=self, _next_seq=self.next_seq)
    self.seq = 0
  def next_seq(self):
    self.seq += 1
    return self.seq
  def reply(self, kind, seq, value):
    self.responses.append(Message(kind, seq, 0, memoryview(json.dumps(value).encode())))
  def send(self, kind, seq, parts, timeout=None):
    self.types.append(kind)
    self.session.handle(Message(kind, seq, 0, memoryview(b''.join(parts))))
  def recv(self, timeout=None):
    return self.responses.pop(0)


def cache_assets(env, cache):
  cache.mkdir()
  for kind, path in zip(usb.KINDS, (env.bundle, env.model), strict=True):
    (cache / env.value[kind]['sha256']).write_bytes(path.read_bytes())


def test_sender_receiver_full_exchange_cached_offline_and_reconnect(env, monkeypatch):
  cache = env.tmp / 'comma-cache'
  cache_assets(env, cache)
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('cached upload attempted network'))
  wire, progress = Wire(), []
  sender.upload(wire.client, env.value, env.public, cache, lambda: True, lambda *a: progress.append(a))
  assert usb.registered(env.root, env.value)
  assert wire.types[0] == usb.BEGIN and wire.types[-1] == usb.FINISH
  assert wire.types.count(usb.CHUNK) >= 4 and progress[-1] == ('verifying', 1.)
  assert (env.root / 'current').resolve() == env.previous
  wire.session.close()
  cached = Wire()
  sender.upload(cached.client, env.value, env.public, cache, lambda: True, lambda *a: None)
  assert cached.types == [usb.BEGIN]
  cached.session.close()


def test_sender_onroad_transition_aborts_before_registration(env):
  cache = env.tmp / 'comma-cache'
  cache_assets(env, cache)
  wire = Wire()
  with pytest.raises(ConnectionError, match='offroad'):
    sender.upload(wire.client, env.value, env.public, cache, lambda: wire.types.count(usb.CHUNK) < 2, lambda *a: None)
  wire.session.close()
  assert not (env.root / 'local-releases').exists()
  assert (env.root / 'current').resolve() == env.previous


def test_sender_checks_ack_identity(env):
  wire = Wire()
  original = wire.recv
  def wrong(timeout=None):
    message = original(timeout)
    message.seq += 1
    return message
  wire.recv = wrong
  with pytest.raises(ValueError, match='acknowledgement'):
    sender.upload(wire.client, env.value, env.public, env.tmp / 'cache2', lambda: True, lambda *a: None)
  wire.session.close()


def test_receiver_disabled_without_enrollment_and_busy_during_activation(env):
  (env.root / 'updater/usb-experiment-public.pem').unlink()
  wire = Wire()
  wire.send(p.Msg.HELLO_REQ, 1, [b'{}'])
  assert json.loads(bytes(wire.recv().payload))[usb.CAPABILITY] is False
  wire.send(usb.BEGIN, 2, [json.dumps(env.value).encode()])
  assert json.loads(bytes(wire.recv().payload))['ok'] is False
  enroll.install(env.root, env.public)
  wire.session.gate.worker = NS(is_alive=lambda: True)
  wire.send(usb.BEGIN, 3, [json.dumps(env.value).encode()])
  assert json.loads(bytes(wire.recv().payload))['ok'] is False


def test_enrollment_idempotent_preserves_official_key_and_rejects_rotation(env):
  original = (env.root / 'updater/release-signing-public.pem').read_bytes()
  enroll.install(env.root, env.public)
  other = env.tmp / 'other.pem'
  other.write_text(ECC.generate(curve='Ed25519').public_key().export_key(format='PEM'))
  with pytest.raises(ValueError, match='different experiment key'):
    enroll.install(env.root, other)
  other.write_text(env.key.export_key(format='PEM'))
  with pytest.raises(ValueError, match='never a private key'):
    enroll.install(env.root, other)
  assert original == (env.root / 'updater/release-signing-public.pem').read_bytes()


def test_signed_release_tool_and_comma_selection(env, monkeypatch):
  private = env.tmp / 'private.pem'
  private.write_text(env.key.export_key(format='PEM'))
  output = env.tmp / 'published'
  prepare.prepare(env.bundle, env.model, private, env.value['profile'], env.value['bundle']['url'],
                  env.value['model']['url'], output)
  value = json.loads((output / 'usb_host_release.json').read_text())
  assert usb.verify(value, output / 'usb_host_public.pem') == env.value
  monkeypatch.setattr(jetson_model, 'read_spec', lambda: {'sha256': value['model']['sha256'], 'nbytes': value['model']['size']})
  path = output / 'host_release.json'
  assert jetson_model.selected_release(path, required=True) == value
  (output / 'local_host_release.json').write_text('{}')
  with pytest.raises(ValueError, match='one experimental'):
    jetson_model.selected_release(path, required=True)


def test_boot_client_missing_capability_fails_without_sending(env, monkeypatch):
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: env.value)
  with pytest.raises(ValueError, match='receiver'):
    startup.wait_for_boot_update(None, {'carrot_host': 'jetson', startup.CAPABILITY: True}, None,
                                lambda: True, lambda *a, **k: None)


def test_matching_usb_boot_does_not_download_or_require_offroad(env, monkeypatch):
  transfer(env)
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: env.value)
  calls = []
  client = NS(t=NS(send_json=lambda *a: calls.append(a)), _next_seq=lambda: 1,
              state=lambda: {'carrot_update': {'state': 'ready'}})
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.CAPABILITY: True, 'usb_source_commit': 'b' * 40}
  with pytest.raises(ConnectionError, match='reconnecting'):
    startup.wait_for_boot_update(client, peer, None, lambda: True, lambda *a, **k: None)
  assert len(calls) == 1 and calls[0][-1] == env.value


@pytest.mark.parametrize('failure', ['hash', 'truncated', 'oversized', 'redirect', 'onroad'])
def test_comma_download_failure_cannot_replace_cache(env, monkeypatch, failure):
  data = env.model.read_bytes()
  if failure == 'hash':
    data = b'X' + data[1:]
  elif failure == 'truncated':
    data = data[:-1]
  elif failure == 'oversized':
    data += b'X'
  class Response(io.BytesIO):
    url = 'http://example.invalid/file' if failure == 'redirect' else env.value['model']['url']
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: Response(data))
  cache = env.tmp / 'downloads'
  cache.mkdir()
  target = cache / env.value['model']['sha256']
  target.write_bytes(b'previous-cache')
  with pytest.raises((ValueError, ConnectionError)):
    sender.asset(env.value['model'], cache, lambda: failure != 'onroad')
  assert target.read_bytes() == b'previous-cache'
  assert not list(cache.glob('*.part'))


def test_comma_download_success_is_atomic_and_reused(env, monkeypatch):
  data, urls = env.model.read_bytes(), []
  class Response(io.BytesIO):
    url = env.value['model']['url']
  def fetch(url, **kwargs):
    urls.append(url)
    return Response(data)
  monkeypatch.setattr(sender.urllib.request, 'urlopen', fetch)
  cache = env.tmp / 'download-good'
  path = sender.asset(env.value['model'], cache, lambda: True)
  assert path.read_bytes() == data
  assert sender.asset(env.value['model'], cache, lambda: True) == path
  assert urls == [Response.url]


def test_large_assets_are_not_rehashed_for_each_selection_heartbeat(env, monkeypatch):
  transfer(env)
  import local_release
  monkeypatch.setattr(local_release, 'verify_assets', lambda *a: pytest.fail('heartbeat rehashed model'))
  for _ in range(4):
    assert update.selected_commit(env.value) == env.value['source_commit']


def test_disk_budget_and_unsafe_catalog_fail_closed(env, monkeypatch):
  monkeypatch.setattr(usb.shutil, 'disk_usage', lambda _: NS(free=1))
  with pytest.raises(OSError, match='Insufficient'):
    usb.Receiver(env.root).begin(env.value)
  assert not list((env.root / 'usb-incoming').glob('receive-*'))
  outside = env.tmp / 'outside'
  outside.mkdir()
  catalog = env.root / 'local-releases'
  catalog.symlink_to(outside)
  (outside / env.value['profile']).mkdir()
  with pytest.raises(ValueError, match='root-owned'):
    usb.Receiver(env.root).begin(env.value)


def test_wire_rejects_oversized_manifest_and_replayed_chunks(env):
  wire = Wire()
  wire.send(usb.BEGIN, 1, [bytes(65537)])
  assert json.loads(bytes(wire.recv().payload))['ok'] is False
  wire.send(usb.BEGIN, 2, [json.dumps(env.value).encode()])
  assert json.loads(bytes(wire.recv().payload))['ok'] is True
  chunk = usb.HEADER.pack(1, 0) + env.model.read_bytes()[:10]
  wire.send(usb.CHUNK, 3, [chunk])
  assert json.loads(bytes(wire.recv().payload))['offset'] == 10
  wire.send(usb.CHUNK, 3, [chunk])  # duplicate sequence is ignored, not appended
  assert not wire.responses
  assert wire.session.usb_receiver.offsets[1] == 10
  wire.send(usb.CHUNK, 4, [chunk])  # wrong offset with a new sequence is rejected
  assert json.loads(bytes(wire.recv().payload))['ok'] is False
  assert wire.session.usb_receiver is None


def test_cached_asset_mutation_before_activation_is_rejected(env):
  transfer(env)
  catalog = env.root / 'local-releases' / env.value['profile']
  (catalog / 'model.onnx').write_bytes(b'corrupt')
  # Selection is metadata-only; full hash validation must still precede stage.
  assert update.selected_commit(env.value) == env.value['source_commit']
  with pytest.raises(ValueError, match='corrupt'):
    update.stage_manifest(env.value)
  assert not (env.root / 'updates/pending.json').exists()
  assert (env.root / 'current').resolve() == env.previous


def test_boot_client_onroad_cannot_start_usb_upload(env, monkeypatch):
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: env.value)
  monkeypatch.setattr(startup, 'RELEASE', env.tmp / 'host_release.json')
  (env.tmp / 'usb_host_public.pem').write_bytes(env.public.read_bytes())
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: NS(get_bool=lambda k: k == 'IsOnroad')))
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.CAPABILITY: True, 'usb_source_commit': 'a' * 40}
  wire = Wire()
  with pytest.raises(ConnectionError, match='offroad'):
    startup.wait_for_boot_update(wire.client, peer, None, lambda: True, lambda *a, **k: None)
  assert not wire.types
