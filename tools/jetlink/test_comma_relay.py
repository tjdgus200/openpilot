"""Two trust domains, interrupted delivery, transactional rollback and client IO."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace as NS

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
import pytest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[2] / 'third_party/jetlink')]
import boot_update as boot
import relay_manifest as wire
import relay_release as relay
import update_host as update
from jetlink import protocol as p
from test_update_host import manifest as official_manifest


def sign(value, key):
  value['signature'] = base64.b64encode(eddsa.new(key, 'rfc8032').sign(wire.canonical(value))).decode()
  return value


def asset(data, name):
  return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
          'url': 'https://github.com/tjdgus200/openpilot/releases/download/test/' + name}


@pytest.fixture
def env(tmp_path, monkeypatch):
  if getattr(os, 'geteuid', lambda: -1)() != 0:
    pytest.skip('Requires isolated Linux root-owned filesystem fixtures')
  root = tmp_path / 'runtime'
  root.mkdir()
  (root / 'trust').mkdir()
  key = ECC.generate(curve='Ed25519')
  public = key.public_key().export_key(format='PEM')
  (root / 'trust/comma-relay-public.pem').write_text(public)
  previous = root / 'releases' / ('a' * 40)
  previous.mkdir(parents=True)
  (previous / 'SOURCE_COMMIT').write_text('a' * 40)
  (root / 'current').symlink_to(previous)
  (root / 'cache').mkdir()
  (root / 'cache/last-loaded.json').write_text('official engine')
  model = b'fixture model bytes'
  stream = io.BytesIO()
  with tarfile.open(fileobj=stream, mode='w:gz') as archive:
    files = {'SOURCE_COMMIT': b'b' * 40,
             'openpilot/selfdrive/modeld/jetlink/jetson_model.json':
               json.dumps({'sha256': hashlib.sha256(model).hexdigest(), 'nbytes': len(model)}).encode()}
    for name, data in files.items():
      member = tarfile.TarInfo(name)
      member.size, member.mode = len(data), 0o644
      archive.addfile(member, io.BytesIO(data))
  bundle = stream.getvalue()
  manifest = sign({'format': 1, 'delivery': wire.DELIVERY, 'source_commit': 'b' * 40, 'storage_format': 1,
                   'runtime': wire.RUNTIME, 'bundle': asset(bundle, 'host.tar.gz'), 'model': asset(model, 'model.onnx')}, key)
  monkeypatch.setattr(update, 'ROOT', root)
  monkeypatch.setattr(update, 'PROTECTED_MARKER', root / 'absent')
  monkeypatch.setattr(update, 'check_runtime', lambda _: None)
  monkeypatch.setattr(update, 'probe_release', lambda _: None)
  monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: NS(returncode=3))
  monkeypatch.setattr(boot, 'READY', root / 'ready')
  monkeypatch.setattr(boot, 'BOOT_ID', root / 'boot-id')
  boot.BOOT_ID.write_text('this-boot')
  import pwd
  monkeypatch.setattr(pwd, 'getpwnam', lambda _: NS(pw_uid=0, pw_gid=0))
  return NS(root=root, previous=previous, model=model, bundle=bundle, manifest=manifest, key=key, public=public)


def upload(receiver, env):
  for index, data in enumerate((env.bundle, env.model)):
    receiver.chunk(wire.HEADER.pack(bytes.fromhex(receiver.id), index, 0) + data)
  receiver.finish()


def test_independent_signature_and_strict_channel(env):
  relay.verify(env.root, env.manifest)
  assert update.selected_commit(env.manifest) == 'b' * 40
  with pytest.raises(ValueError):
    update.validate_manifest(env.manifest)
  with pytest.raises(ValueError):
    update.verify_signature(env.manifest)  # user key cannot authorize official path
  with pytest.raises(ValueError):
    relay.verify(env.root, official_manifest())
  for change in ({'source_commit': 'c' * 40}, {'storage_format': 2}, {'delivery': 'local'}):
    with pytest.raises(ValueError):
      relay.verify(env.root, {**env.manifest, **change})
  forged = sign(dict(env.manifest), ECC.generate(curve='Ed25519'))
  with pytest.raises(ValueError):
    relay.Receiver(env.root, forged)
  assert not (env.root / 'personal').exists()


def test_partial_transfer_resumes_without_publishing_and_rejects_wrong_offsets(env):
  receiver = relay.Receiver(env.root, env.manifest)
  identity = bytes.fromhex(receiver.id)
  receiver.chunk(wire.HEADER.pack(identity, 0, 0) + env.bundle[:10])
  restarted = relay.Receiver(env.root, env.manifest)
  assert restarted.status()['offsets'] == [10, 0]
  for payload in (wire.HEADER.pack(identity, 0, 0) + b'x',
                  wire.HEADER.pack(b'0' * 32, 0, 10) + b'x',
                  wire.HEADER.pack(identity, 9, 10) + b'x',
                  wire.HEADER.pack(identity, 1, 0) + b'x' * (len(env.model) + 1)):
    with pytest.raises(ValueError):
      restarted.chunk(payload)
  with pytest.raises((ValueError, FileNotFoundError)):
    restarted.finish()
  assert (env.root / 'current').resolve() == env.previous
  assert not (env.root / 'updates/pending.json').exists()


def test_corrupt_object_is_deleted_for_clean_retry_and_symlink_is_rejected(env):
  receiver = relay.Receiver(env.root, env.manifest)
  receiver.chunk(wire.HEADER.pack(bytes.fromhex(receiver.id), 0, 0) + b'x' * len(env.bundle))
  with pytest.raises(ValueError, match='checksum'):
    receiver.finish()
  assert receiver.status()['offsets'][0] == 0
  (receiver.path / 'model.part').symlink_to(env.root / 'cache/last-loaded.json')
  with pytest.raises(ValueError):
    receiver.status()
  assert (env.root / 'cache/last-loaded.json').read_text() == 'official engine'


def test_custom_activation_isolated_and_official_restore_reuses_official_cache(env, monkeypatch):
  upload(relay.Receiver(env.root, env.manifest), env)
  update.stage_manifest(env.manifest)
  assert (env.root / 'current').resolve() == env.previous
  update.activate()
  personal = relay.release_path(env.root, env.manifest)
  assert (env.root / 'current').resolve() == personal
  assert update.release_cache(personal) == relay.cache_path(env.root, env.manifest)
  assert (env.root / 'cache/last-loaded.json').read_text() == 'official engine'
  assert not (env.root / 'cache/models').exists()
  assert update.current_matches(env.manifest)
  official = official_manifest()
  monkeypatch.setattr(update, 'verify_signature', lambda _: None)
  update.atomic_json(env.root / 'updates/pending.json', official)
  update.activate()
  assert (env.root / 'current').resolve() == env.previous
  assert update.current_matches(official)
  assert not update.current_matches(env.manifest)
  assert (env.root / 'cache/last-loaded.json').read_text() == 'official engine'


def test_failed_probe_and_power_loss_preserve_previous_release(env, monkeypatch):
  upload(relay.Receiver(env.root, env.manifest), env)
  update.stage_manifest(env.manifest)
  def reject(_):
    raise ValueError('synthetic TRT failure')
  monkeypatch.setattr(update, 'probe_release', reject)
  update.activate()
  assert (env.root / 'current').resolve() == env.previous
  assert json.loads((env.root / 'updates/status.json').read_text())['state'] == 'rejected'
  update.stage_manifest(env.manifest)
  class PowerLoss(BaseException):
    pass
  def power_loss(_):
    raise PowerLoss()
  monkeypatch.setattr(update, 'probe_release', power_loss)
  with pytest.raises(PowerLoss):
    update.activate()
  assert (env.root / 'updates/transaction.json').exists()
  update.activate()
  assert (env.root / 'current').resolve() == env.previous
  assert not (env.root / 'updates/transaction.json').exists()


def test_same_commit_different_trust_domain_is_not_ready(env, monkeypatch):
  value = sign({**env.manifest, 'source_commit': 'a' * 40}, env.key)
  assert not update.current_matches(value)
  personal = relay.release_path(env.root, value)
  personal.mkdir(parents=True)
  (personal / 'SOURCE_COMMIT').write_text('a' * 40)
  (personal / '.comma-relay.json').write_text(json.dumps(value))
  (env.root / 'current').unlink()
  (env.root / 'current').symlink_to(personal)
  monkeypatch.setattr(update, 'verify_signature', lambda _: None)
  assert not update.current_matches(official_manifest())


def test_boot_protocol_rejects_upload_without_signed_selection_and_cancel(env):
  replies = []
  transport = NS(send_json=lambda kind, seq, obj: replies.append((kind, obj)), send=lambda *a: None)
  gate = boot.Gate()
  session = boot.BootstrapSession(transport, gate)
  session.handle(NS(seq=1, msg_type=wire.QUERY, payload=b'bad'))
  assert replies[-1][0] == p.Msg.ERROR
  session.handle(NS(seq=2, msg_type=boot.MESSAGE, payload=json.dumps(env.manifest).encode()))
  gate.tick()
  assert gate.state == 'waiting_upload' and gate.worker is None
  identity = bytes.fromhex(wire.identity(env.manifest))
  for seq, (index, data) in enumerate(enumerate((env.bundle, env.model)), start=3):
    session.handle(NS(seq=seq, msg_type=wire.CHUNK, payload=wire.HEADER.pack(identity, index, 0) + data))
    assert replies[-1][0] == wire.ACK
  session.handle(NS(seq=5, msg_type=wire.FINISH, payload=identity))
  assert replies[-1][1]['complete']
  session.handle(NS(seq=6, msg_type=wire.CANCEL, payload=identity))
  gate.tick()
  assert gate.selected is None and gate.worker is None and not gate.done
  assert (env.root / 'current').resolve() == env.previous


def test_archive_path_traversal_and_no_space_fail_before_activation(env, monkeypatch):
  stream = io.BytesIO()
  with tarfile.open(fileobj=stream, mode='w:gz') as archive:
    member = tarfile.TarInfo('../../escape')
    member.size = 1
    archive.addfile(member, io.BytesIO(b'x'))
  env.bundle = stream.getvalue()
  env.manifest = sign({**env.manifest, 'bundle': asset(env.bundle, 'host.tar.gz')}, env.key)
  upload(relay.Receiver(env.root, env.manifest), env)
  with pytest.raises(ValueError, match='Unsafe'):
    update.stage_manifest(env.manifest)
  assert (env.root / 'current').resolve() == env.previous
  monkeypatch.setattr(relay.shutil, 'disk_usage', lambda _: NS(free=0))
  with pytest.raises(OSError, match='space'):
    relay.Receiver(env.root, env.manifest)


def test_client_sender_roundtrip_to_boot_receiver(env, monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import relay as client_relay
  public = env.root / 'public.pem'
  public.write_text(env.public)
  monkeypatch.setattr(client_relay, 'PUBLIC_KEY', public)
  monkeypatch.setattr(client_relay, 'CACHE', env.root / 'comma-cache')
  sender = client_relay.Sender(env.manifest, lambda: True, parked=lambda: True)
  sender.path.mkdir(parents=True)
  (sender.path / 'bundle').write_bytes(env.bundle)
  (sender.path / 'model').write_bytes(env.model)
  sender.worker = NS(is_alive=lambda: False)
  gate = boot.Gate()
  gate.select(env.manifest)
  replies = []
  session = boot.BootstrapSession(NS(send_json=lambda kind, seq, obj:
                                    replies.append(NS(msg_type=kind, seq=seq, payload=json.dumps(obj).encode()))), gate)
  sequence = iter(range(1, 1000))
  def expect(kind, seq, timeout):
    reply = replies.pop(0)
    assert (reply.msg_type, reply.seq) == (kind, seq)
    return reply
  client = NS(_next_seq=lambda: next(sequence), _expect=expect,
              t=NS(send=lambda kind, seq, parts, **k: session.handle(NS(msg_type=kind, seq=seq, payload=b''.join(parts)))))
  assert sender.pump(client) == 'verifying'
  assert gate.receiver.complete and sender.complete
  gate.tick()
  gate.worker.join(5)
  gate.tick()
  assert gate.done and boot.runtime_ready()
  assert (env.root / 'cache/last-loaded.json').read_text() == 'official engine'


def test_client_refuses_unprovisioned_host_and_tampered_selection(env, monkeypatch, tmp_path):
  from openpilot.common import jetson_model
  from openpilot.selfdrive.modeld.jetlink import startup
  release = tmp_path / 'host_release.json'
  value = env.manifest
  monkeypatch.setattr(jetson_model, 'read_spec', lambda: {'sha256': value['model']['sha256'], 'nbytes': value['model']['size']})
  release.with_name('comma_host_release.json').write_text(json.dumps(value))
  release.with_name('comma-relay-public.pem').write_text(env.public)
  monkeypatch.setattr(startup, 'RELEASE', release)
  with pytest.raises(ValueError, match='One-time'):
    startup.wait_for_boot_update(None, {'carrot_host': 'jetson', startup.CAPABILITY: True}, None, lambda: True, lambda *a: None)
  value = {**value, 'source_commit': 'c' * 40}
  release.with_name('comma_host_release.json').write_text(json.dumps(value))
  with pytest.raises(ValueError):
    jetson_model.selected_release(release)  # never fall back to local/NAS


def test_boot_selection_cannot_switch_mid_transaction_or_activate_after_disconnect(env, monkeypatch):
  gate = boot.Gate()
  gate.select(env.manifest)
  gate.worker = NS(is_alive=lambda: True)
  with pytest.raises(ValueError, match='in progress'):
    gate.select(official_manifest())
  assert gate.selected == env.manifest
  gate.worker = None
  gate.received = 0
  monkeypatch.setattr(update, 'stage_manifest', lambda *a, **k: None)
  monkeypatch.setattr(update, 'activate', lambda: pytest.fail('Disconnected update must not activate'))
  gate._install(env.manifest)
  assert not gate.done and (env.root / 'current').resolve() == env.previous


@pytest.mark.parametrize('onroad', [True, False])
def test_client_onroad_can_use_existing_release_but_cannot_upload(onroad):
  from openpilot.selfdrive.modeld.jetlink import startup
  calls = []
  sender = NS(parked=lambda: False, id='a' * 64, pump=lambda _: pytest.fail('No onroad transfer'))
  client = NS(t=NS(send_json=lambda *a: calls.append('select'), send=lambda *a: calls.append('cancel')),
              _next_seq=lambda: 1, state=lambda: {'carrot_update': {'state': 'waiting_upload' if onroad else 'ready'}})
  if onroad:
    with pytest.raises(InterruptedError):
      startup._wait(client, {}, None, lambda: True, lambda *a, **k: None, lambda _: None, {}, sender)
    assert calls == ['select', 'cancel']
  else:
    startup._wait(client, {}, None, lambda: True, lambda *a, **k: None, lambda _: None, {}, sender)
    assert calls == ['select']


def test_personal_bootstrap_preserves_official_key_and_rejects_key_replacement(env, tmp_path):
  import install_comma_relay as installer
  import prepare_comma_release as prepare
  here = Path(__file__).parent
  bundle, model, key = tmp_path / 'host.tar.gz', tmp_path / 'model.onnx', tmp_path / 'private.pem'
  model.write_bytes(env.model)
  key.write_text(env.key.export_key(format='PEM'))
  with tarfile.open(bundle, 'w:gz') as archive:
    for name, data in {'SOURCE_COMMIT': b'b' * 40,
                       'openpilot/selfdrive/modeld/jetlink/jetson_model.json':
                         json.dumps({'sha256': env.manifest['model']['sha256'], 'nbytes': len(env.model)}).encode()}.items():
      member = tarfile.TarInfo(name)
      member.size = len(data)
      archive.addfile(member, io.BytesIO(data))
    for name in (*installer.HELPERS, 'install_comma_relay.py', 'install_local_experiment.py', 'release-signing-public.pem'):
      archive.add(here / name, arcname='tools/jetlink/' + name)
  package = tmp_path / 'bootstrap'
  prepare.prepare(bundle, model, key, package, env.manifest['bundle']['url'], env.manifest['model']['url'])
  runtime = env.root
  (runtime / 'updater').mkdir()
  official = (here / 'release-signing-public.pem').read_bytes()
  (runtime / 'updater/release-signing-public.pem').write_bytes(official)
  (runtime / 'updater/update_host.py').write_text('old helper')
  (runtime / 'boot-update-required').write_text('enabled')
  (runtime / 'protected-runtime.json').write_text('{}')
  # Same mathematical key but provision exactly the canonical package bytes.
  (runtime / 'trust/comma-relay-public.pem').write_bytes((package / 'comma-relay-public.pem').read_bytes())
  fingerprint = hashlib.sha256((package / 'comma-relay-public.pem').read_bytes()).hexdigest()
  installer.install(package, runtime, fingerprint)
  installer.install(package, runtime, fingerprint)  # idempotent re-run
  assert (runtime / 'current').resolve() == env.previous
  assert (runtime / 'updater/release-signing-public.pem').read_bytes() == official
  assert (runtime / 'updater/before-comma-relay/update_host.py').read_text() == 'old helper'
  assert not (runtime / 'personal').exists()  # receiver installation does not activate/stage
  (runtime / 'trust/comma-relay-public.pem').write_text('different key')
  with pytest.raises(ValueError, match='rotation'):
    installer.install(package, runtime, fingerprint)
  (package / 'boot_update.py').write_text('modified helper')
  with pytest.raises(ValueError, match='differs'):
    installer.inspect(package, fingerprint)


def test_release_builder_flattens_only_internal_regular_links():
  from build_host_bundle import relay_member
  stream = io.BytesIO()
  with tarfile.open(fileobj=stream, mode='w') as archive:
    file = tarfile.TarInfo('source/data.json')
    file.mode, file.size = 0o664, 2
    archive.addfile(file, io.BytesIO(b'{}'))
    for name, target in (('source/link.json', 'data.json'), ('escape', '../outside'), ('cycle', 'cycle')):
      link = tarfile.TarInfo(name)
      link.type, link.linkname = tarfile.SYMTYPE, target
      archive.addfile(link)
  stream.seek(0)
  with tarfile.open(fileobj=stream) as archive:
    member, data = relay_member(archive, archive.getmember('source/link.json'))
    assert member.isfile() and member.mode == 0o644 and data.read() == b'{}'
    for name in ('escape', 'cycle'):
      with pytest.raises(ValueError):
        relay_member(archive, archive.getmember(name))


def test_cached_transfer_can_restage_after_engine_cache_is_owned_by_runtime(env, monkeypatch):
  import pwd
  monkeypatch.setattr(pwd, 'getpwnam', lambda _: NS(pw_uid=65534, pw_gid=65534))
  upload(relay.Receiver(env.root, env.manifest), env)
  update.stage_manifest(env.manifest)
  update.stage_manifest(env.manifest)
  assert relay.cache_path(env.root, env.manifest).stat().st_uid == 65534
  relay.verify_staged(env.root, env.manifest)
  assert (env.root / 'cache/last-loaded.json').read_text() == 'official engine'
