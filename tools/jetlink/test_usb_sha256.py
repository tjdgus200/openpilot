"""Real files, hashes and activation transactions over an in-memory USB transport.

No installed services, vehicle CAN, TensorRT execution or device access.
"""
import builtins
import copy
import hashlib
from http.client import HTTPResponse
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_local_experiment import env as _catalog_fixture, archive, select_and_activate
import boot_update as boot
import install_usb_receiver as enroll
import prepare_usb_release as prepare
import update_host as update
import usb_release as usb
from jetlink import protocol as p
from openpilot.common import jetson_model
from openpilot.selfdrive.modeld.jetlink import startup
from tools.jetlink import usb_release_client as sender  # noqa: TID251 -- standalone updater

env = _catalog_fixture  # Reuse the official root-owned filesystem fixture.


@pytest.fixture
def installed(env, monkeypatch):
  monkeypatch.setattr(usb.os, 'sync', lambda: None)
  env.value = prepare.prepare(env.package / 'host.tar.gz', env.package / 'model.onnx', 'test-usb',
                              'https://example.invalid/host.tar.gz', 'https://example.invalid/model.onnx', env.temporary / 'public')
  env.official_key = (env.root / 'updater/release-signing-public.pem').read_bytes()
  enroll.install(env.root)
  return env


class Wire:
  def __init__(self):
    self.responses, self.kinds, self.seq = [], [], 0
    self.session = boot.BootstrapSession(self, boot.Gate())
    self.client = NS(t=self, _next_seq=self.next_seq,
                     state=lambda: {'carrot_update': {'state': 'ready'}})

  def next_seq(self):
    self.seq += 1
    return self.seq

  def send_json(self, kind, seq, value):
    if kind == startup.MESSAGE:
      self.session.handle(NS(msg_type=kind, seq=seq, payload=json.dumps(value).encode()))
    else:
      self.responses.append(NS(msg_type=kind, seq=seq, payload=json.dumps(value).encode()))

  def send(self, kind, seq, parts, **_):
    self.kinds.append(kind)
    self.session.handle(NS(msg_type=kind, seq=seq, payload=b''.join(parts)))

  def recv(self, **_):
    return self.responses.pop(0)


def cache_assets(env):
  cache = env.temporary / 'cache'
  cache.mkdir(exist_ok=True)
  for kind, name in zip(usb.KINDS, usb.NAMES, strict=True):
    (cache / env.value[kind]['sha256']).write_bytes((env.package / name).read_bytes())
  return cache


def upload(env, allowed=lambda: True):
  wire = Wire()
  try:
    sender.upload(wire.client, env.value, cache_assets(env), allowed, lambda *a: None)
  finally:
    wire.session.close()
  return wire


def deny_crypto(monkeypatch):
  original = builtins.__import__
  def check(name, *args, **kwargs):
    if name.startswith('Crypto'):
      pytest.fail('SHA-only USB must not need signing keys or Crypto imports')
    return original(name, *args, **kwargs)
  monkeypatch.setattr(builtins, '__import__', check)


def test_keyless_upload_registration_activation_and_cached_reuse(installed, monkeypatch):
  deny_crypto(monkeypatch)
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('cached upload attempted HTTP'))
  wire = upload(installed)
  assert wire.kinds[0] == usb.BEGIN and wire.kinds[-1] == usb.FINISH
  assert (installed.root / 'current').resolve() == installed.previous
  assert not (installed.root / 'updates/pending.json').exists()
  assert select_and_activate(installed.value).done
  assert (installed.root / 'current/SOURCE_COMMIT').read_text() == 'b' * 40
  assert upload(installed).kinds == [usb.BEGIN]
  assert (installed.root / 'updater/release-signing-public.pem').read_bytes() == installed.official_key


def test_legacy_protected_image_enrolls_before_usb_selection(env, monkeypatch):
  monkeypatch.setattr(usb.os, 'sync', lambda: None)
  factory_marker = env.root / 'boot-update-required'
  factory_marker.unlink()
  root = env.temporary / 'factory-app'
  systemd = root / 'etc/systemd/system'
  systemd.mkdir(parents=True)
  (root / 'etc/carrot-jetlink-protected.json').write_text('{}')
  (systemd / 'carrot-jetlink-update-apply.service').write_text(
    'ExecStart=/opt/carrot-jetlink/venv/bin/python /opt/carrot-jetlink/updater/update_host.py activate\n')
  for unit in ('carrot-jetlink', 'carrot-jetlink-hud'):
    (systemd / (unit + '.service')).write_text('[Service]\nExecStart=/bin/true\n')
  previous = (env.root / 'current').resolve()
  key = env.root / 'updater/release-signing-public.pem'
  key_bytes = key.read_bytes()
  assert not usb.sha256_enabled(env.root)
  assert not boot.enabled()
  enroll.install_legacy_image(root, env.root)
  assert boot.enabled() and usb.sha256_enabled(env.root)
  assert key.read_bytes() == key_bytes
  assert (env.root / 'current').resolve() == previous
  assert not (root / 'opt/carrot-jetlink/boot-update-required').exists()
  for unit in ('carrot-jetlink', 'carrot-jetlink-hud'):
    dropin = (systemd / (unit + '.service.d/usb-bootstrap.conf')).read_text()
    assert 'After=carrot-jetlink-update-apply.service' in dropin
    assert 'wait_for_runtime()' in dropin
  wire = Wire()
  try:
    wire.session.handle(NS(msg_type=p.Msg.HELLO_REQ, seq=1, payload=b'{}'))
    hello = json.loads(wire.recv().payload)
    assert hello[startup.CAPABILITY] is True
    assert hello[usb.SHA256_CAPABILITY] and hello[usb.RESUME_CAPABILITY]
    assert hello['loaded'] is None
  finally:
    wire.session.close()
  env.value = prepare.prepare(env.package / 'host.tar.gz', env.package / 'model.onnx', 'test-usb',
                              'https://example.invalid/host.tar.gz', 'https://example.invalid/model.onnx', env.temporary / 'public')
  upload(env)
  assert select_and_activate(env.value).done
  assert (env.root / 'current/SOURCE_COMMIT').read_text() == 'b' * 40


@pytest.mark.parametrize('reject_second', [False, True])
def test_distinct_model_sources_switch_back_or_retain_prior_release(installed, monkeypatch, reject_second):
  import hashlib
  env = installed
  upload(env)
  assert select_and_activate(env.value).done
  first_value, first_package = env.value, env.package
  first_loaded = (env.root / 'cache/last-loaded.json').read_text()
  second = env.temporary / 'second-model'
  second.mkdir()
  model = second / 'model.onnx'
  model.write_bytes(b'second-distinct-model')
  identity = {'sha256': hashlib.sha256(model.read_bytes()).hexdigest(), 'size': model.stat().st_size}
  local = {'format': 1, 'delivery': 'local', 'profile': 'test-usb-second', 'model': identity}
  archive(second / 'host.tar.gz', 'c' * 40, model.read_bytes(), local)
  env.value = prepare.prepare(second / 'host.tar.gz', model, 'test-usb-second',
      'https://example.invalid/second-host.tar.gz', 'https://example.invalid/second-model.onnx', second)
  env.package = second
  assert env.value['source_commit'] != first_value['source_commit']
  assert env.value['model'] != first_value['model']
  boot.BOOT_ID.write_text('boot-2')
  assert not boot.runtime_ready()
  upload(env)
  probe = update.probe_release
  if reject_second:
    def reject(_):
      (env.root / 'cache/last-loaded.json').write_text('rejected-second-model')
      raise RuntimeError('Synthetic second-model inference failure')
    monkeypatch.setattr(update, 'probe_release', reject)
  gate = select_and_activate(env.value)
  if reject_second:
    assert not gate.done and gate.state == 'failed' and not boot.runtime_ready()
    assert (env.root / 'current/SOURCE_COMMIT').read_text() == first_value['source_commit']
    assert (env.root / 'cache/last-loaded.json').read_text() == first_loaded
  else:
    assert gate.done and boot.runtime_ready()
    assert (env.root / 'current/SOURCE_COMMIT').read_text() == 'c' * 40
  monkeypatch.setattr(update, 'probe_release', probe)
  env.value, env.package = first_value, first_package
  boot.BOOT_ID.write_text('boot-3')
  assert upload(env).kinds == [usb.BEGIN]  # Reuse verified first-model assets.
  assert select_and_activate(env.value).done
  assert (env.root / 'current/SOURCE_COMMIT').read_text() == first_value['source_commit']
  assert (env.root / 'updater/release-signing-public.pem').read_bytes() == env.official_key


def test_prepare_both_assets_before_sending_any_file_bytes(installed, monkeypatch):
  wire, prepared = Wire(), []
  original = sender.asset
  def prepare(item, cache, allowed, **kwargs):
    assert usb.CHUNK not in wire.kinds, 'A network/hash wait must not split the USB files'
    prepared.append(item['sha256'])
    return original(item, cache, allowed, **kwargs)
  monkeypatch.setattr(sender, 'asset', prepare)
  try:
    sender.upload(wire.client, installed.value, cache_assets(installed), lambda: True, lambda *a: None)
    assert prepared == [installed.value[k]['sha256'] for k in usb.KINDS]
    assert usb.registered(installed.root, installed.value)
  finally:
    wire.session.close()


def test_cached_asset_hash_check_reports_progress_without_network(installed, monkeypatch):
  cache = cache_assets(installed)
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('cached asset used network'))
  reports = []
  item = installed.value['model']
  assert sender.asset(item, cache, lambda: True, progress=lambda *args: reports.append(args)).is_file()
  assert reports[0] == ('checking_files', 0)
  assert reports[-1] == ('checking_files', item['size'])


def test_download_reports_data_before_a_full_block(tmp_path, monkeypatch):
  data = b'x' * 512
  item = {'url': 'https://example.invalid/model', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
  reports = []
  class Response(io.BytesIO):
    url = item['url']

    def read(self, _size):
      pytest.fail('waiting for a full network block hides the first bytes')

    def read1(self, size):
      return super().read(min(size, 1 if self.tell() == 0 else size))

  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: Response(data))
  assert sender.asset(item, tmp_path, lambda: True, progress=lambda *a: reports.append(a)).read_bytes() == data
  assert reports[:2] == [('connecting_download', 0), ('downloading', 1)]


def test_http_timeouts_within_one_block_still_advance_resume(tmp_path, monkeypatch):
  size, burst = 256 * 1024, 64 * 1024
  data = b'x' * size
  item = {'url': 'https://example.invalid/model', 'size': size, 'sha256': hashlib.sha256(data).hexdigest()}
  offsets = []

  class FragmentedSocket(io.RawIOBase):
    def __init__(self, wire):
      self.wire = io.BytesIO(wire)

    def readable(self):
      return True

    def readinto(self, buffer):
      received = self.wire.read(min(8192, len(buffer)))
      if not received:
        raise TimeoutError('Simulated socket inactivity')
      buffer[:len(received)] = received
      return len(received)

  def fetch(request, **kw):
    offset = 0 if isinstance(request, str) else int(request.get_header('Range').removeprefix('bytes=').rstrip('-'))
    offsets.append(offset)
    status = 206 if offset else 200
    header = (f'HTTP/1.1 {status} OK\r\nContent-Length: {size - offset}\r\n'
              f'Content-Range: bytes {offset}-{size - 1}/{size}\r\n\r\n').encode()
    raw = io.BufferedReader(FragmentedSocket(header + data[offset:offset + burst]))
    response = HTTPResponse(NS(makefile=lambda _: raw))
    response.begin()
    response.url = item['url']
    return response

  monkeypatch.setattr(sender.urllib.request, 'urlopen', fetch)
  for expected in (burst, 2 * burst, 3 * burst):
    with pytest.raises(ConnectionError):
      sender.asset(item, tmp_path, lambda: True)
    assert (tmp_path / (item['sha256'] + '.part')).stat().st_size == expected
  assert sender.asset(item, tmp_path, lambda: True).read_bytes() == data
  assert offsets == [0, burst, 2 * burst, 3 * burst]


@pytest.mark.parametrize('drop_usb', [False, True])
def test_boot_retries_http_timeout_without_resetting_usb(installed, monkeypatch, drop_usb):
  from contextlib import nullcontext
  wire, states, connections = Wire(), [], []
  connected = [True]
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: object()))
  monkeypatch.setattr(startup, 'Permission', lambda *a, **kw: NS(allowed=lambda: True))
  monkeypatch.setattr(startup, 'bulk_scheduling', nullcontext)
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **kw: installed.value)
  original = sender.upload
  monkeypatch.setattr(sender, 'upload', lambda client, value, cache, allowed, progress, **kw:
                      original(client, value, installed.temporary / 'retry-cache', allowed, progress, **kw))
  def fetch(request, **kw):
    url = request if isinstance(request, str) else request.full_url
    connections.append(url)
    if len(connections) == 1:
      raise TimeoutError('No HTTP response')
    name = next(name for kind, name in zip(usb.KINDS, usb.NAMES, strict=True) if installed.value[kind]['url'] == url)
    response = io.BytesIO((installed.package / name).read_bytes())
    response.url = url
    return response
  monkeypatch.setattr(sender.urllib.request, 'urlopen', fetch)
  def sleep(_):
    assert not usb.registered(installed.root, installed.value)
    if drop_usb:
      connected[0] = False
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.SHA256_CAPABILITY: True,
          usb.RESUME_CAPABILITY: True}
  try:
    with pytest.raises(ConnectionError, match='disconnected' if drop_usb else 'reconnecting'):
      startup.wait_for_boot_update(wire.client, peer, None, lambda: connected[0],
                                  lambda *a, **kw: states.append(kw['host_update']['state']), sleep=sleep)
    assert states[:3] == ['checking_usb', 'connecting_download', 'retrying_download']
    assert wire.kinds.count(usb.BEGIN_RESUME) == (1 if drop_usb else 2)
    assert len(connections) == (1 if drop_usb else 3)
    assert usb.registered(installed.root, installed.value) is not drop_usb
  finally:
    wire.session.close()


@pytest.mark.parametrize('recover', [True, False])
@pytest.mark.parametrize('resumable', [True, False])
def test_transfer_permission_gap_waits_without_rebuilding_usb(installed, monkeypatch, recover, resumable):
  from contextlib import nullcontext
  wire, permitted, connected, states = Wire(), [True], [True], []
  interrupted = []
  original_upload, original_exchange = sender.upload, sender.exchange
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: object()))
  monkeypatch.setattr(startup, 'Permission', lambda *a, **kw: NS(allowed=lambda: permitted[0]))
  monkeypatch.setattr(startup, 'bulk_scheduling', nullcontext)
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **kw: installed.value)
  monkeypatch.setattr(sender, 'upload', lambda client, value, cache, allowed, progress, **kw:
                      original_upload(client, value, cache_assets(installed), allowed, progress, **kw))
  def exchange(client, kind, *args, **kwargs):
    reply = original_exchange(client, kind, *args, **kwargs)
    if kind == usb.CHUNK and not interrupted:
      permitted[0] = False
      interrupted.append(list(wire.kinds))
    return reply
  monkeypatch.setattr(sender, 'exchange', exchange)
  def resume(_):
    assert wire.kinds == interrupted[0]  # No additional file write without permission.
    assert not usb.registered(installed.root, installed.value)
    assert wire.session.gate.selected is None
    permitted[0], connected[0] = recover, recover
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.SHA256_CAPABILITY: True, usb.RESUME_CAPABILITY: resumable,
          'engine_state': 'none', 'loaded': None}
  try:
    with pytest.raises(ConnectionError, match='reconnecting' if recover else 'USB setup'):
      startup.wait_for_boot_update(wire.client, peer, None, lambda: connected[0],
                                  lambda *a, **kw: states.append(kw['host_update']['state']), sleep=resume)
    assert 'waiting_park' in states
    assert wire.kinds.count(usb.BEGIN_RESUME if resumable else usb.BEGIN) == (2 if recover else 1)
    expected_chunks = 1 + int(recover) * (1 if resumable else 2)
    assert wire.kinds.count(usb.CHUNK) == expected_chunks
    assert (usb.FINISH in wire.kinds) is recover
    assert usb.registered(installed.root, installed.value) is recover
    assert (installed.root / 'current').resolve() == installed.previous
  finally:
    wire.session.close()


def test_unsigned_usb_does_not_bypass_official_nas_signature(installed, monkeypatch):
  from test_update_host import manifest
  calls = []
  def reject(value):
    calls.append(value)
    raise ValueError('signature required')
  monkeypatch.setattr(update, 'verify_signature', reject)
  with pytest.raises(ValueError, match='signature required'):
    update.selected_commit(manifest())
  assert len(calls) == 1


def test_official_return_uses_existing_signed_https_path(installed, monkeypatch):
  upload(installed)
  assert select_and_activate(installed.value).done
  model = b'official return model'
  bundle = installed.temporary / 'official.tar.gz'
  archive(bundle, 'c' * 40, model)
  model_path = installed.temporary / 'official.onnx'
  model_path.write_bytes(model)
  official = {'format': 1, 'source_commit': 'c' * 40, 'runtime': usb.RUNTIME, 'storage_format': 1,
              'bundle': prepare.identity(bundle, 'https://upload.shind0.synology.me/models/test/host.tar.gz'),
              'model': prepare.identity(model_path, 'https://upload.shind0.synology.me/models/test/model.onnx')}
  verified, downloads = [], []
  monkeypatch.setattr(update, 'verify_signature', lambda value: verified.append(value))
  def download(url, **_):
    downloads.append(url)
    response = io.BytesIO(bundle.read_bytes() if url.endswith('host.tar.gz') else model)
    response.url = url
    return response
  monkeypatch.setattr(update.urllib.request, 'urlopen', download)
  boot.BOOT_ID.write_text('boot-2')
  assert select_and_activate(official).done
  assert (installed.root / 'current/SOURCE_COMMIT').read_text() == 'c' * 40
  assert verified and all(value == official for value in verified)
  assert downloads == [official[k]['url'] for k in usb.KINDS]
  assert (installed.root / 'updater/release-signing-public.pem').read_bytes() == installed.official_key


@pytest.mark.parametrize('fault', ['missing', 'permissions', 'symlink', 'owner', 'boolean', 'unknown', 'oversize'])
def test_local_policy_is_required_before_allocating_or_advertising(installed, fault):
  import os
  policy = installed.root / 'updater' / usb.SHA256_POLICY
  if fault == 'missing':
    policy.unlink()
  elif fault == 'permissions':
    policy.chmod(0o666)
  elif fault == 'symlink':
    saved = policy.with_suffix('.saved')
    policy.rename(saved)
    policy.symlink_to(saved)
  elif fault == 'owner':
    os.chown(policy, 1000, 1000)
  else:
    policy.write_text({'boolean': '{"format":true,"integrity":"sha256","transport":"usb"}',
                       'unknown': '{"format":1,"integrity":"other","transport":"usb"}', 'oversize': ' ' * 257}[fault])
  assert not usb.sha256_enabled(installed.root)
  wire = Wire()
  wire.session.handle(NS(msg_type=p.Msg.HELLO_REQ, seq=1, payload=b'{}'))
  assert json.loads(wire.recv().payload)[usb.SHA256_CAPABILITY] is False
  with pytest.raises(ValueError, match='local owner policy'):
    usb.Receiver(installed.root).begin(installed.value)
  assert not (installed.root / 'usb-incoming').exists()


@pytest.mark.parametrize('fault', ['hash', 'length', 'offset', 'incomplete', 'profile_collision', 'no_space', 'probe'])
def test_failed_transfer_or_probe_preserves_current(installed, monkeypatch, fault):
  receiver = usb.Receiver(installed.root)
  value = copy.deepcopy(installed.value)
  if fault == 'hash':
    value['model']['sha256'] = '0' * 64
  if fault == 'length':
    value['model']['size'] += 1
  if fault == 'no_space':
    monkeypatch.setattr(usb.shutil, 'disk_usage', lambda _: NS(free=0))
    with pytest.raises(OSError, match='space'):
      receiver.begin(value)
    assert receiver.directory is None
    return
  if fault == 'profile_collision':
    upload(installed)
    value['source_commit'] = 'c' * 40
    with pytest.raises(ValueError, match='another release'):
      receiver.begin(value)
    return
  receiver.begin(value)
  if fault == 'offset':
    with pytest.raises(ValueError, match='offset'):
      receiver.chunk(usb.HEADER.pack(0, 1) + b'x')
  elif fault != 'incomplete':
    for index, name in enumerate(usb.NAMES):
      data = (installed.package / name).read_bytes()
      receiver.chunk(usb.HEADER.pack(index, 0) + data)
  if fault == 'probe':
    receiver.finish()
    def fail(_):
      raise RuntimeError('synthetic probe failure')
    monkeypatch.setattr(update, 'probe_release', fail)
    gate = select_and_activate(value)
    assert not gate.done and gate.state == 'failed' and gate.telemetry()['failure_stage'] == 'installing'
    assert (installed.root / 'cache/last-loaded.json').read_text() == 'original-model'
  else:
    with pytest.raises(ValueError, match='checksum/length'):
      receiver.finish()
    assert not (installed.root / 'local-releases' / value['profile']).exists()
  receiver.close()
  assert (installed.root / 'current').resolve() == installed.previous
  assert not list((installed.root / 'usb-incoming').iterdir())


def test_interrupted_transfer_and_reconnect_restart_without_activation(installed, monkeypatch):
  wire = Wire()
  with pytest.raises(ConnectionError, match='offroad'):
    sender.upload(wire.client, installed.value, cache_assets(installed), lambda: len(wire.kinds) < 2, lambda *a: None)
  assert usb.FINISH not in wire.kinds
  wire.session.close()
  assert (installed.root / 'current').resolve() == installed.previous
  assert not (installed.root / 'local-releases').exists()
  assert usb.registered(installed.root, installed.value) is False
  upload(installed)
  assert usb.registered(installed.root, installed.value)


@pytest.mark.parametrize('bad', [{'profile': '../outside'}, {'runtime': {}}, {'format': True}, {'signature': ''},
                               {'source_commit': 123}, {'storage_format': True}])
def test_manifest_rejects_path_traversal_abi_and_extra_fields(installed, bad):
  value = {**installed.value, **bad}
  with pytest.raises(ValueError):
    usb.Receiver(installed.root).begin(value)
  assert not (installed.root / 'usb-incoming').exists()


def test_selection_is_opt_in_pins_model_and_rejects_local_conflict(installed, monkeypatch):
  path = installed.temporary / 'public/host_release.json'
  monkeypatch.setattr(jetson_model, 'read_spec', lambda: {'sha256': installed.value['model']['sha256'],
                                                       'nbytes': installed.value['model']['size']})
  assert jetson_model.selected_release(path, required=True) == installed.value
  local = path.with_name('local_host_release.json')
  local.write_text('{}')
  with pytest.raises(ValueError, match='Choose one'):
    jetson_model.selected_release(path, required=True)
  local.unlink()
  monkeypatch.setattr(jetson_model, 'read_spec', lambda: {'sha256': '0' * 64, 'nbytes': 1})
  with pytest.raises(ValueError, match='branch model'):
    jetson_model.selected_release(path, required=True)


def test_startup_checks_capability_and_offroad_before_usb_transfer(installed, monkeypatch):
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: installed.value)
  wire = Wire()
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.SHA256_CAPABILITY: False}
  with pytest.raises(ValueError, match='locally enable'):
    startup.wait_for_boot_update(wire.client, peer, None, lambda: True, lambda *a, **k: None)
  assert not wire.kinds
  peer.update({usb.SHA256_CAPABILITY: True, 'usb_source_commit': 'a' * 40})
  values = {'IsOffroad': False, 'IsOnroad': True}
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: NS(get_bool=values.get)))
  original = sender.upload
  monkeypatch.setattr(sender, 'upload', lambda client, value, cache, allowed, progress:
                      original(client, value, cache_assets(installed), allowed, progress))
  reports = []
  def offroad(_):
    assert not wire.kinds
    values.update(IsOffroad=True, IsOnroad=False)
  with pytest.raises(ConnectionError, match='reconnecting'):
    startup.wait_for_boot_update(wire.client, peer, None, lambda: True,
                                lambda *a, **k: reports.append(k['host_update']['state']), sleep=offroad)
  assert reports[0] == 'waiting_park' and 'transferring' in reports
  assert usb.registered(installed.root, installed.value)
  wire.session.close()


def test_receiver_rejects_while_activation_worker_is_running(installed):
  wire = Wire()
  wire.session.gate.worker = NS(is_alive=lambda: True)
  wire.send(usb.BEGIN, 1, [json.dumps(installed.value).encode()])
  reply = json.loads(wire.recv().payload)
  assert reply['ok'] is False and 'retained' in reply['error']
  assert not (installed.root / 'usb-incoming').exists()


def test_enrollment_preflight_preserves_unrelated_trust_and_current(installed):
  updater = installed.root / 'updater'
  before = {name: (updater / name).read_bytes() for name in enroll.FILES}
  (updater / 'boot_update.py').chmod(0o666)
  with pytest.raises(ValueError, match='root-owned'):
    enroll.install(installed.root)
  assert before == {name: (updater / name).read_bytes() for name in enroll.FILES}
  assert (updater / 'release-signing-public.pem').read_bytes() == installed.official_key
  assert (installed.root / 'current').resolve() == installed.previous


@pytest.mark.parametrize('registered', [False, True])
def test_matching_current_source_requires_read_only_registration_check(installed, monkeypatch, registered):
  if registered:
    upload(installed)
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: installed.value)
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.SHA256_CAPABILITY: True,
          usb.CHECK_CAPABILITY: True, 'usb_source_commit': installed.value['source_commit']}
  values = {'IsOffroad': False, 'IsOnroad': True}
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: NS(get_bool=values.get)))
  original = sender.upload
  monkeypatch.setattr(sender, 'upload', lambda client, value, cache, allowed, progress:
                      original(client, value, cache_assets(installed), allowed, progress))
  wire, reports = Wire(), []
  def offroad(_):
    assert wire.kinds == [usb.CHECK]  # No allocation before offroad, even with matching source.
    assert not (installed.root / 'usb-incoming').exists()
    values.update(IsOffroad=True, IsOnroad=False)
  with pytest.raises(ConnectionError, match='reconnecting'):
    startup.wait_for_boot_update(wire.client, peer, None, lambda: True,
                                lambda *a, **k: reports.append(k['host_update']['state']), sleep=offroad)
  assert reports[0] == 'checking_usb'
  if registered:
    assert wire.kinds == [usb.CHECK] and 'waiting_park' not in reports
  else:
    assert wire.kinds[0] == usb.CHECK and 'waiting_park' in reports and usb.FINISH in wire.kinds
  wire.session.close()


@pytest.mark.parametrize('fault', ['hash', 'length', 'redirect', 'interrupted'])
def test_download_failure_never_publishes_a_cache_entry(installed, monkeypatch, fault):
  item = installed.value['model']
  data = (installed.package / 'model.onnx').read_bytes()
  if fault == 'hash':
    data = b'x' * len(data)
  if fault == 'length':
    data += b'x'
  response = io.BytesIO(data)
  response.url = 'http://example.invalid/file' if fault == 'redirect' else item['url']
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: response)
  cache = installed.temporary / 'failed-cache'
  checks = []
  def allowed():
    checks.append(1)
    return fault != 'interrupted' or len(checks) < 2
  with pytest.raises((ValueError, ConnectionError)):
    sender.asset(item, cache, allowed)
  assert not list(cache.iterdir())


@pytest.mark.parametrize('failure', ['timeout', 'short_body', 'permission'])
def test_interrupted_download_resumes_only_missing_bytes(tmp_path, monkeypatch, failure):
  data = b'12345678'
  item = {'url': 'https://example.invalid/model', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
  partial = tmp_path / (item['sha256'] + '.part')
  monkeypatch.setattr(sender, 'BLOCK', 4)

  class InterruptedResponse(io.BytesIO):
    url = item['url']

    def read1(self, size):
      if failure == 'timeout' and self.tell() == 4:
        raise TimeoutError
      return super().read(size)

  initial_body = data if failure == 'permission' else data[:4]
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: InterruptedResponse(initial_body))
  permitted_checks = iter([True, True, False])
  allowed = (lambda: next(permitted_checks)) if failure == 'permission' else (lambda: True)
  with pytest.raises((TimeoutError, ConnectionError)):
    sender.asset(item, tmp_path, allowed)
  assert partial.read_bytes() == data[:4]

  def resume(request, **kwargs):
    assert request.get_header('Range') == 'bytes=4-'
    response = io.BytesIO(data[4:])
    response.url = item['url']
    response.status = 206
    response.headers = {'Content-Range': 'bytes 4-7/8'}
    return response

  monkeypatch.setattr(sender.urllib.request, 'urlopen', resume)
  path = sender.asset(item, tmp_path, lambda: True)
  assert path.read_bytes() == data
  assert not partial.exists()

  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('HTTP cache miss'))
  assert sender.asset(item, tmp_path, lambda: True) == path


@pytest.mark.parametrize('reply', ['range_ignored', 'wrong_range', 'wrong_payload'])
def test_resume_response_and_complete_asset_are_validated(tmp_path, monkeypatch, reply):
  data = b'12345678'
  item = {'url': 'https://example.invalid/model', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
  path = tmp_path / item['sha256']
  partial = path.with_suffix('.part')
  partial.write_bytes(data[:4])
  response = io.BytesIO(data if reply == 'range_ignored' else b'bad!')
  response.url = item['url']
  response.status = 200 if reply == 'range_ignored' else 206
  response.headers = {'Content-Range': 'bytes 3-6/8' if reply == 'wrong_range' else 'bytes 4-7/8'}
  monkeypatch.setattr(sender.urllib.request, 'urlopen', lambda *a, **k: response)

  if reply == 'range_ignored':
    assert sender.asset(item, tmp_path, lambda: True).read_bytes() == data
  else:
    with pytest.raises(ValueError, match='download (range|hash)'):
      sender.asset(item, tmp_path, lambda: True)
    assert not path.exists()
  assert not partial.exists()


@pytest.mark.parametrize('fault', ['sequence', 'kind', 'size', 'json'])
def test_unrelated_or_malformed_acknowledgement_cannot_complete(installed, fault):
  reply = NS(msg_type=usb.ACK, seq=1, payload=b'{"ok":true,"complete":true}')
  if fault == 'sequence':
    reply.seq = 2
  if fault == 'kind':
    reply.msg_type = p.Msg.STATE_RESP
  if fault == 'size':
    reply.payload = b' ' * 65537
  if fault == 'json':
    reply.payload = b'[]'
  client = NS(_next_seq=lambda: 1, t=NS(send=lambda *a, **k: None, recv=lambda **k: reply))
  with pytest.raises(ValueError):
    sender.available(client, installed.value, lambda: True)


def test_boot_heartbeat_does_not_rehash_large_registered_assets(installed, monkeypatch):
  upload(installed)
  monkeypatch.setattr(update, 'digest', lambda _: pytest.fail('heartbeat hashed a large asset'))
  for _ in range(20):
    assert update.selected_commit(installed.value) == installed.value['source_commit']


def test_bootstrap_copies_receiver_without_enabling_policy(installed):
  import install_updates
  image = installed.temporary / 'image'
  install_updates.bootstrap(image, Path(__file__).parent)
  updater = image / 'opt/carrot-jetlink/updater'
  assert (updater / 'usb_release.py').read_bytes() == (Path(__file__).with_name('usb_release.py')).read_bytes()
  assert not (updater / usb.SHA256_POLICY).exists()
  assert not usb.sha256_enabled(updater.parent)


def test_enrollment_retains_only_immediately_previous_helpers(installed):
  updater = installed.root / 'updater'
  before = (updater / 'boot_update.py').read_bytes() + b'\n# preceding installation\n'
  (updater / 'boot_update.py').write_bytes(before)
  enroll.install(installed.root)
  assert (updater / 'before-usb-experiments/boot_update.py').read_bytes() == before
  assert not (updater / 'before-usb-experiments/before-usb-experiments').exists()


def test_disconnect_removes_partial_transfer_without_registering(installed):
  wire = Wire()
  sender.exchange(wire.client, usb.BEGIN, installed.value, lambda: True)
  sender.exchange(wire.client, usb.CHUNK, usb.HEADER.pack(0, 0) + b'x', lambda: True, raw=True)
  incoming = wire.session.usb_receiver.directory
  assert incoming.is_dir()
  wire.session.close()
  wire.session.close()
  assert not incoming.exists()
  assert not usb.registered(installed.root, installed.value)
  assert (installed.root / 'current').resolve() == installed.previous
  assert not (installed.root / 'updates/pending.json').exists()


@pytest.mark.parametrize('drop', [usb.CHUNK, usb.FINISH])
def test_negotiated_resume_uses_written_offsets_after_lost_ack(installed, monkeypatch, drop):
  wire = Wire()
  reply = wire.send_json
  chunks = []
  send = wire.send

  def track(kind, seq, parts, **kwargs):
    if kind == usb.CHUNK:
      chunks.append(usb.HEADER.unpack_from(parts[0]))
    send(kind, seq, parts, **kwargs)

  def drop_ack(kind, seq, value):
    # Simulate the link breaking while the Jetson writes an ACK, after
    # accepting the request and before the sender sees the result.
    if kind == usb.ACK and wire.kinds[-1] == drop:
      raise TimeoutError('simulated ACK loss')
    reply(kind, seq, value)

  monkeypatch.setattr(wire, 'send', track)
  monkeypatch.setattr(wire, 'send_json', drop_ack)
  cache = cache_assets(installed)
  with pytest.raises(TimeoutError, match='ACK loss'):
    sender.upload(wire.client, installed.value, cache, lambda: True, lambda *a: None, resume=True)
  if drop == usb.CHUNK:
    assert chunks == [(0, 0)]
    assert not usb.registered(installed.root, installed.value)
  else:
    assert usb.registered(installed.root, installed.value)
  assert wire.session.gate.selected is None
  assert (installed.root / 'current').resolve() == installed.previous

  wire.session.close(reconnect=True)
  wire.session.attach(wire)
  monkeypatch.setattr(wire, 'send_json', reply)
  monkeypatch.setattr(sender, 'asset', lambda item, *a, **k: cache / item['sha256']
                      if item == installed.value['model'] else pytest.fail('already sent bundle was prepared again'))
  states = []
  sender.upload(wire.client, installed.value, cache, lambda: True,
                lambda *s: states.append(s), resume=True)
  assert chunks == [(0, 0), (1, 0)]
  if drop == usb.CHUNK:
    bundle, model = (installed.value[k]['size'] for k in usb.KINDS)
    assert states[1] == ('transferring', bundle / (bundle + model))
  else:
    assert states == [('checking_usb', None)]
  assert usb.registered(installed.root, installed.value)
  assert (installed.root / 'current').resolve() == installed.previous
  assert not (installed.root / 'updates/pending.json').exists()
  wire.session.close()


@pytest.mark.parametrize('resume,change_identity', [(False, False), (True, True)])
def test_legacy_sender_and_different_package_do_not_reuse_partial(installed, resume, change_identity):
  wire = Wire()
  value = installed.value
  begin = sender.exchange(wire.client, usb.BEGIN_RESUME, value, lambda: True)
  assert begin == {'ok': True, 'complete': False, 'offsets': [0, 0]}
  sender.exchange(wire.client, usb.CHUNK, usb.HEADER.pack(0, 0) + b'x', lambda: True, raw=True)
  wire.session.close(reconnect=True)
  wire.session.attach(wire)
  if change_identity:
    value = {**value, 'source_commit': 'c' * 40}
  response = sender.exchange(wire.client, usb.BEGIN_RESUME if resume else usb.BEGIN, value, lambda: True)
  assert response.get('offsets') == ([0, 0] if resume else None)
  assert (wire.session.usb_receiver.directory / usb.NAMES[0]).stat().st_size == 0
  wire.session.close()


@pytest.mark.parametrize('offsets', [None, [], [0], [True, 0], [-1, 0], [0, 1 << 40]])
def test_invalid_resume_offset_stops_before_cache_or_transfer(installed, monkeypatch, offsets):
  calls = []
  def reply(_, kind, *a, **kw):
    calls.append(kind)
    return {'complete': False, 'offsets': offsets}
  monkeypatch.setattr(sender, 'exchange', reply)
  monkeypatch.setattr(sender, 'asset', lambda *a, **k: pytest.fail('invalid offsets reached asset cache'))
  with pytest.raises(ValueError, match='resume offsets'):
    sender.upload(None, installed.value, None, lambda: True, lambda *a: None, resume=True)
  assert calls == [usb.BEGIN_RESUME]


def test_corrupted_resumed_prefix_never_registers_and_next_attempt_starts_clean(installed):
  wire = Wire()
  value, cache = installed.value, cache_assets(installed)
  sender.exchange(wire.client, usb.BEGIN_RESUME, value, lambda: True)
  sender.exchange(wire.client, usb.CHUNK, usb.HEADER.pack(0, 0) + b'?', lambda: True, raw=True)
  wire.session.close(reconnect=True)
  wire.session.attach(wire)
  with pytest.raises(ValueError, match='rejected USB update'):
    sender.upload(wire.client, value, cache, lambda: True, lambda *a: None, resume=True)
  assert not usb.registered(installed.root, value)
  assert wire.session.usb_receiver is None
  response = sender.exchange(wire.client, usb.BEGIN_RESUME, value, lambda: True)
  assert response['offsets'] == [0, 0]
  wire.session.close()


def test_resume_sends_only_the_missing_tail_of_model(installed, monkeypatch):
  wire = Wire()
  value, cache = installed.value, cache_assets(installed)
  model = (installed.package / usb.NAMES[1]).read_bytes()
  prefix = len(model) // 2
  assert prefix > 0
  sender.exchange(wire.client, usb.BEGIN_RESUME, value, lambda: True)
  for index, data in enumerate(((installed.package / usb.NAMES[0]).read_bytes(), model[:prefix])):
    sender.exchange(wire.client, usb.CHUNK, usb.HEADER.pack(index, 0) + data, lambda: True, raw=True)
  wire.session.close(reconnect=True)
  wire.session.attach(wire)
  send = wire.send
  sent = []
  def capture(kind, seq, parts, **kw):
    if kind == usb.CHUNK:
      sent.append(parts[0])
    send(kind, seq, parts, **kw)
  monkeypatch.setattr(wire, 'send', capture)
  sender.upload(wire.client, value, cache, lambda: True, lambda *a: None, resume=True)
  assert sent == [usb.HEADER.pack(1, prefix) + model[prefix:]]
  assert usb.registered(installed.root, value)
  wire.session.close()


def test_revoked_local_policy_prevents_completed_transfer_registration(installed):
  wire = Wire()
  sender.exchange(wire.client, usb.BEGIN, installed.value, lambda: True)
  for index, name in enumerate(usb.NAMES):
    sender.exchange(wire.client, usb.CHUNK,
                    usb.HEADER.pack(index, 0) + (installed.package / name).read_bytes(), lambda: True, raw=True)
  incoming = wire.session.usb_receiver.directory
  (installed.root / 'updater' / usb.SHA256_POLICY).unlink()
  with pytest.raises(ValueError, match='rejected USB update'):
    sender.exchange(wire.client, usb.FINISH, {}, lambda: True)
  assert not incoming.exists()
  assert not usb.registered(installed.root, installed.value)
  assert (installed.root / 'current').resolve() == installed.previous


def test_onroad_transition_after_upload_does_not_request_activation(installed, monkeypatch):
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: installed.value)
  values = {'IsOffroad': True, 'IsOnroad': False}
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: NS(get_bool=values.get)))
  original = sender.upload
  def transition(client, value, cache, allowed, progress):
    original(client, value, cache_assets(installed), allowed, progress)
    values.update(IsOffroad=False, IsOnroad=True)
  monkeypatch.setattr(sender, 'upload', transition)
  wire, connected, reports = Wire(), [True], []
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, usb.SHA256_CAPABILITY: True,
          'usb_source_commit': 'a' * 40}
  with pytest.raises(ConnectionError, match='USB setup'):
    startup.wait_for_boot_update(wire.client, peer, None, lambda: connected[0],
                                lambda *a, **k: reports.append(k['host_update']['state']),
                                sleep=lambda _: connected.__setitem__(0, False))
  assert 'waiting_park' in reports
  assert usb.registered(installed.root, installed.value)
  assert wire.session.gate.selected is None
  assert not (installed.root / 'updates/pending.json').exists()
  assert (installed.root / 'current').resolve() == installed.previous
  wire.session.close()
