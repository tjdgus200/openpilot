"""Linux-only PC service/file lifecycle fault injection; all OS calls mocked."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import pytest
sys.path.insert(0, str(Path(__file__).parent))
import park_update as host
import boot_update as boot
import update_host as update
from test_update_host import manifest

@pytest.mark.skipif(sys.platform != 'linux', reason='Linux service ownership and symlinks')
@pytest.mark.parametrize('expire_at', ['before_probe', 'during_probe', 'before_swap', 'never'])
def test_lost_park_permission_keeps_pending_previous_runtime_and_cache(tmp_path, monkeypatch, expire_at):
  monkeypatch.setattr(update, 'ROOT', tmp_path)
  monkeypatch.setattr(update, 'verify_signature', lambda _: None)
  monkeypatch.setattr(update.os, 'sync', lambda: None)
  monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: NS(returncode=3))
  old, new = tmp_path / 'releases' / ('d' * 40), tmp_path / 'releases' / ('a' * 40)
  old.mkdir(parents=True)
  new.mkdir()
  (tmp_path / 'current').symlink_to(old)
  (tmp_path / 'cache').mkdir()
  cache = tmp_path / 'cache/last-loaded.json'
  cache.write_text('original')
  update.atomic_json(tmp_path / 'updates/pending.json', manifest())
  calls = []
  def allowed():
    calls.append(1)
    return expire_at == 'never' or len(calls) < {'before_probe': 1, 'during_probe': 2, 'before_swap': 3}[expire_at]
  def probe(_, **kwargs):
    cache.write_text('synthetic candidate')
  monkeypatch.setattr(update, 'probe_release', probe)
  if expire_at == 'never':
    update.activate(allowed=allowed)
    assert (tmp_path / 'current').resolve() == new
    assert not (tmp_path / 'updates/pending.json').exists()
  else:
    with pytest.raises(update.ActivationDeferred):
      update.activate(allowed=allowed)
    assert (tmp_path / 'current').resolve() == old and cache.read_text() == 'original'
    assert (tmp_path / 'updates/pending.json').exists()
    assert not (tmp_path / 'current.next').exists()
  assert not (tmp_path / 'updates/transaction.json').exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux root worker ownership')
@pytest.mark.parametrize('code', [0, 1, 2])
def test_unconfirmed_runtime_retirement_never_opens_boot_usb(tmp_path, monkeypatch, code):
  monkeypatch.setattr(host, 'supported', lambda: True)
  monkeypatch.setattr(update, 'PROTECTED_MARKER', tmp_path / 'absent')
  monkeypatch.setattr(host, 'RECOVERY', tmp_path / 'recovery')
  monkeypatch.setattr(boot, 'BOOT_ID', tmp_path / 'boot')
  boot.BOOT_ID.write_text('id')
  monkeypatch.setattr(host, 'read_request', lambda *a: {'boot_id': 'id'})
  monkeypatch.setattr(host.os, 'umask', lambda _: None)
  monkeypatch.setattr(host.time, 'sleep', lambda _: None)
  monkeypatch.setattr(host.subprocess, 'run', lambda *a, **k: NS(returncode=code))
  monkeypatch.setattr(boot, 'boot', lambda **k: pytest.fail('USB gate opened before retirement'))
  with pytest.raises(RuntimeError, match='retirement'):
    host.enter()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux private file identity')
def test_request_reader_rejects_symlink_public_permissions_and_oversize(tmp_path):
  path = tmp_path / 'request'
  path.write_text('{}')
  path.chmod(0o600)
  assert host.read_request(path, os.getuid()) == {}
  with pytest.raises(ValueError):
    host.read_request(path, os.getuid() + 1)
  path.chmod(0o644)
  with pytest.raises(ValueError):
    host.read_request(path, os.getuid())
  path.chmod(0o600)
  path.write_bytes(b' ' * (host.MAX_BYTES + 1))
  with pytest.raises(ValueError):
    host.read_request(path, os.getuid())
  link = tmp_path / 'link'
  link.symlink_to(path)
  with pytest.raises(OSError):
    host.read_request(link, os.getuid())


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux root worker')
def test_restarted_worker_resumes_only_empty_gate_without_reusing_old_permission(tmp_path, monkeypatch):
  monkeypatch.setattr(host, 'supported', lambda: True)
  monkeypatch.setattr(update, 'PROTECTED_MARKER', tmp_path / 'absent')
  monkeypatch.setattr(host, 'RECOVERY', tmp_path / 'recovery')
  monkeypatch.setattr(host, 'REQUEST_FILE', tmp_path / 'unprivileged-request')
  monkeypatch.setattr(boot, 'BOOT_ID', tmp_path / 'boot')
  monkeypatch.setattr(boot, 'READY', tmp_path / 'ready')
  boot.BOOT_ID.write_text('id')
  boot.READY.write_text('stale ready')
  monkeypatch.setattr(host, 'read_request', lambda path, uid:
      {'boot_id': 'id'} if path == host.RECOVERY and uid == 0 else pytest.fail('Remembered permit reused'))
  monkeypatch.setattr(host.os, 'umask', lambda _: None)
  monkeypatch.setattr(host.time, 'sleep', lambda _: None)
  calls = []
  monkeypatch.setattr(host.subprocess, 'run', lambda command, **k: (calls.append(command) or NS(returncode=3)))
  def gate(**kwargs):
    assert kwargs == {'parked': True} and not boot.READY.exists()
    calls.append('empty-gate')
  monkeypatch.setattr(boot, 'boot', gate)
  host.enter()
  assert calls[0] == ['systemctl', 'stop', 'carrot-jetlink', 'carrot-jetlink-hud']
  assert calls[-1] == 'empty-gate'
