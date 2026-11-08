"""PC ownership/lease/reboot fault injection; no vehicle or live services."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from openpilot.common import jetlink_maintenance as maintenance
from openpilot.selfdrive.modeld.jetlink import update_permission, startup
import boot_update as boot
import update_host as update
import park_update as host
from test_boot_update import env  # noqa: F401 -- isolated updater fixture
from test_update_host import manifest


@pytest.fixture
def ownership(tmp_path, monkeypatch):
  now = [100.]
  monkeypatch.setattr(maintenance, 'REQUEST', tmp_path / 'request')
  monkeypatch.setattr(maintenance, 'ACK', tmp_path / 'ack')
  monkeypatch.setattr(maintenance, 'identity', lambda: {'boot': '1' * 36, 'release': 'a' * 40})
  owner = maintenance.Owner(clock=lambda: now[0])
  lease = maintenance.Lease(clock=lambda: now[0])
  monkeypatch.setattr(maintenance, '_lease', lease)
  return owner, lease, now


def test_only_completed_native_output_grants_speed_independent_permission(ownership):
  owner, lease, now = ownership
  assert not lease.allowed()
  assert not owner.held(external=True)  # Even a disconnected HUD is not proof.
  assert not lease.allowed()
  now[0] += .21
  assert owner.held(external=False)
  assert not lease.allowed()  # Entering local mode alone is insufficient.
  owner.completed()
  params = NS(get_bool=lambda name: name == 'IsOnroad')
  guard = update_permission.Permission(params, allow_parked=False, allow_internal=True)
  assert guard.allowed() and guard.reason == 'internal_model'
  assert not update_permission.Permission(params, allow_parked=False).allowed()
  now[0] += .26
  assert not lease.allowed()  # Paused/crashed modeld stops approval immediately.


def test_hold_survives_requester_and_modeld_restart_until_explicit_release(ownership):
  owner, lease, now = ownership
  lease.allowed()
  assert owner.held(external=False)
  owner.completed()
  now[0] += 20.
  assert owner.held(external=False)
  restarted = maintenance.Owner(clock=lambda: now[0])
  assert restarted.held(external=False)
  other = maintenance.Lease(clock=lambda: now[0])
  other.release()  # A different token cannot end the acknowledged hold.
  now[0] += .21
  assert restarted.held(external=False)
  lease.release()
  now[0] += .21
  assert not restarted.held(external=False)
  assert not owner.held(external=False)


def test_release_rotates_token_so_old_ack_cannot_approve_a_new_update(ownership):
  owner, lease, now = ownership
  lease.allowed()
  owner.held(external=False)
  owner.completed()
  assert lease.allowed()
  old = lease.token
  lease.release()
  assert lease.token != old
  now[0] += .21
  assert not owner.held(external=False)
  assert not lease.allowed()
  now[0] += .21
  assert not owner.held(external=True)
  assert not lease.allowed()


def test_new_daemon_recovers_held_token_instead_of_stranding_native_model(ownership, monkeypatch):
  owner, lease, now = ownership
  lease.allowed()
  owner.held(external=False)
  monkeypatch.setattr(maintenance, '_lease', None)
  recovered = maintenance.lease()
  recovered.clock = lambda: now[0]
  assert recovered.token == owner.token
  recovered.release()
  now[0] += .21
  assert not owner.held(external=False)


def test_release_is_not_lost_when_modeld_resumes_after_offroad_pause(ownership):
  owner, lease, now = ownership
  lease.allowed()
  owner.held(external=False)
  lease.release()
  now[0] += 30.
  assert not owner.held(external=False)


@pytest.mark.parametrize('bad', ['stale', 'future', 'nan', 'bool', 'token', 'external', 'missing', 'oversized'])
def test_invalid_owner_proof_is_not_permission(ownership, bad):
  owner, lease, now = ownership
  lease.allowed()
  owner.held(external=False)
  owner.completed()
  value = maintenance.read(maintenance.ACK)
  if bad in ('stale', 'future', 'nan', 'bool'):
    value['updated'] = {'stale': 99., 'future': 101., 'nan': float('nan'), 'bool': True}[bad]
  elif bad == 'token': value['token'] = 'b' * 32
  elif bad == 'external': value['internal'] = False
  elif bad == 'missing': value = {}
  elif bad == 'oversized': value['extra'] = 'x' * 5000
  maintenance.write(maintenance.ACK, value)
  assert not lease.allowed()


def test_model_hold_cancels_pending_join_keeps_same_local_inputs_and_outputs(ownership, monkeypatch):
  from test_hotplug_resources import joining_model
  owner, lease, now = ownership
  model = joining_model(monkeypatch)
  model.maintenance = owner
  lease.allowed()
  calls = []
  model.connection = NS(close=lambda: calls.append('cancel'))
  args = [object() for _ in range(4)]
  output = object()
  model.small.run = lambda *actual: (calls.append(actual), output)[1]
  for _ in range(20):
    assert model.run(*args) is output
    assert lease.allowed()
    now[0] += .05
  assert calls[0] == 'cancel' and all(row == tuple(args) for row in calls[1:])
  assert model.client is None and model.small_runs == 23


def proof(release='a', boot_id='1'):
  return {'internal': True, 'token': 'b' * 32, 'boot': boot_id * 36, 'release': release * 40}


def test_verified_update_finishes_but_waits_for_actual_comma_reboot(env, monkeypatch):  # noqa: F811
  value = manifest()
  monkeypatch.setattr(update, 'selected_commit', lambda m: m['source_commit'])
  monkeypatch.setattr(boot.subprocess, 'run', lambda *a, **kw: NS(returncode=0))
  monkeypatch.setattr(update, 'stage_manifest', lambda *a, **kw: None)
  def activate(allowed):
    assert allowed()
    (env / 'current/SOURCE_COMMIT').write_text(value['source_commit'])
  monkeypatch.setattr(update, 'activate', activate)
  gate = boot.Gate()
  gate.select_internal({'manifest': value, 'internal': proof('d')})
  gate.tick()
  gate.worker.join(2)
  gate.tick()
  assert gate.state == 'waiting_comma_reboot' and not gate.done and not boot.runtime_ready()
  # Reload of the host updater and a new modeld with the new sources on the
  # SAME comma boot cannot substitute for the requested device reboot.
  gate = boot.Gate()
  gate.select_internal({'manifest': value, 'internal': proof('a')})
  gate.tick()
  assert gate.state == 'waiting_comma_reboot' and not gate.done
  gate.select_internal({'manifest': value, 'internal': proof('a', '2')})
  gate.tick()
  assert gate.done and boot.runtime_ready()


def test_matching_comma_revision_needs_no_extra_reboot(env, monkeypatch):  # noqa: F811
  value = manifest()
  monkeypatch.setattr(update, 'selected_commit', lambda m: m['source_commit'])
  (env / 'current/SOURCE_COMMIT').write_text(value['source_commit'])
  gate = boot.Gate()
  gate.select_internal({'manifest': value, 'internal': proof()})
  gate.tick()
  assert gate.done and boot.runtime_ready()


def test_runtime_handoff_validates_real_catalog_nbytes_key(env, monkeypatch):  # noqa: F811
  import usb_release
  from openpilot.common import big_model_catalog, jetson_model
  spec = big_model_catalog.model_spec('n16')
  value = manifest()
  value['delivery'] = 'usb'
  value['model'] = {'sha256': spec['sha256'], 'size': spec['nbytes']}
  monkeypatch.setattr(usb_release, 'trusted', lambda *a: None)
  monkeypatch.setattr(update, 'validate_storage_compatibility', lambda _: None)
  monkeypatch.setattr(jetson_model, 'read_spec', lambda _: spec)
  assert host.validate_request({'manifest': value, 'internal': proof()}, env) == value
  value['model']['size'] += 1
  with pytest.raises(ValueError, match='boot-selected'):
    host.validate_request({'manifest': value, 'internal': proof()}, env)


def test_expired_internal_lease_never_activates(env, monkeypatch):  # noqa: F811
  value = manifest()
  now = [100.]
  monkeypatch.setattr(boot.time, 'monotonic', lambda: now[0])
  monkeypatch.setattr(update, 'selected_commit', lambda m: m['source_commit'])
  gate = boot.Gate()
  gate.select_internal({'manifest': value, 'internal': proof()})
  now[0] += 1.
  monkeypatch.setattr(update, 'stage_manifest', lambda *a, **kw: pytest.fail('stale lease installed'))
  gate.tick()
  assert not gate.done and gate.state == 'waiting_park'


def test_empty_gate_accepts_revocation_before_first_native_approval():
  gate = boot.Gate()
  gate.received = 100.
  session = boot.BootstrapSession(NS(), gate)
  session.handle(NS(msg_type=boot.MESSAGE, seq=1, payload=b'{"pause":true}'))
  assert gate.received == 0. and not gate.error and not gate.allowed()


def test_bootstrap_refresh_preserves_existing_boot_policy(tmp_path):
  import install_updates as install
  marker = tmp_path / 'opt/carrot-jetlink/boot-update-required'
  marker.parent.mkdir(parents=True)
  marker.write_text('prior-boot-enabled')
  install.bootstrap(tmp_path, Path(__file__).parent, preserve_policy=True)
  assert marker.read_text() == 'prior-boot-enabled'
  assert (marker.parent / 'updater/boot_update.py').read_bytes() == Path(boot.__file__).read_bytes()


@pytest.mark.parametrize('recent', [True, False])
def test_runtime_internal_handoff_cannot_retire_recent_external_inference(monkeypatch, recent):
  import server
  calls = []
  monkeypatch.setattr(host, 'usb_connection', lambda _: True)
  monkeypatch.setattr(host, 'prepare', lambda *a: calls.append('prepare'))
  monkeypatch.setattr(host, 'launch', lambda *a: calls.append('launch'))
  monkeypatch.setattr(server.time, 'monotonic', lambda: 100.)
  session = server.CarrotSession.__new__(server.CarrotSession)
  session.t, session.last_seq = NS(), 0
  session.last_model_request = 99.5 if recent else 98.
  session._send_json = lambda kind, seq, value: calls.append(value)
  session.handle(NS(msg_type=host.REQUEST, seq=1, received_ns=100_000_000_000,
                    payload=json.dumps({'manifest': {}, 'internal': proof()}).encode()))
  assert calls == ([{'accepted': False}] if recent else ['prepare', {'accepted': True}, 'launch'])


def test_new_boot_gate_uses_internal_proof_even_with_same_registered_package(ownership, monkeypatch):
  owner, lease, _ = ownership
  lease.allowed()
  owner.held(external=False)
  owner.completed()
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **kw: {'source_commit': 'a' * 40})
  monkeypatch.setitem(sys.modules, 'openpilot.common.params',
                      NS(Params=lambda: NS(get_bool=lambda k: k == 'IsOnroad')))
  sent = []
  client = NS(t=NS(send_json=lambda kind, seq, value: sent.append(value)), _next_seq=lambda: 1,
              state=lambda: {'carrot_update': {'state': 'ready'}})
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, maintenance.CAPABILITY: True,
          'engine_state': 'none', 'loaded': None}
  with pytest.raises(startup.RuntimeHandoff):
    startup.wait_for_boot_update(client, peer, None, lambda: True, lambda *a, **kw: None)
  assert sent == [{'manifest': {'source_commit': 'a' * 40}, 'internal': lease.proof}]


def test_native_ownership_through_transport_private_file_and_root_retirement(ownership, env, monkeypatch):  # noqa: F811
  import pwd
  import server
  import usb_release
  from openpilot.common import jetson_model
  from openpilot.selfdrive.modeld.jetlink import park_update as comma
  owner, lease, now = ownership
  value = manifest()
  value['delivery'] = 'usb'
  monkeypatch.setattr(jetson_model, 'selected_release', lambda *a, **kw: value)
  monkeypatch.setattr(jetson_model, 'read_spec', lambda _: {'sha256': value['model']['sha256'], 'nbytes': value['model']['size']})
  monkeypatch.setattr(usb_release, 'trusted', lambda *a: None)
  monkeypatch.setattr(update, 'validate_storage_compatibility', lambda _: None)
  monkeypatch.setattr(host, 'supported', lambda: True)
  prepare = host.prepare
  monkeypatch.setattr(host, 'prepare', lambda payload, stamp, root: prepare(payload, stamp, env, clock=lambda: now[0]))
  monkeypatch.setattr(host, 'usb_connection', lambda _: True)
  monkeypatch.setattr(host, 'DIRECTORY', env / 'private')
  host.DIRECTORY.mkdir()
  monkeypatch.setattr(host, 'REQUEST_FILE', host.DIRECTORY / 'request')
  monkeypatch.setattr(host, 'RECOVERY', env / 'recovery')
  monkeypatch.setattr(host, 'runtime_parked', lambda: pytest.fail('No CAN/Park dependency for native ownership'))
  monkeypatch.setattr(pwd, 'getpwnam', lambda _: NS(pw_uid=host.os.getuid()))
  monkeypatch.setattr(host.os, 'umask', lambda _: None)
  monkeypatch.setattr(host.time, 'monotonic', lambda: now[0])
  actions, replies = [], []
  monkeypatch.setattr(host, 'launch', lambda _: actions.append('launch'))
  monkeypatch.setattr(host.subprocess, 'run', lambda cmd, **kw:
                      (actions.append(cmd) or NS(returncode=0 if cmd[1] == 'stop' else 3)))
  monkeypatch.setattr(boot, 'boot', lambda **kw: actions.append(('empty_gate', kw)))
  session = server.CarrotSession.__new__(server.CarrotSession)
  session.t, session.last_seq = NS(), 0
  session._send_json = lambda kind, seq, obj: replies.append(NS(payload=json.dumps(obj).encode()))
  def send(kind, seq, obj):
    session.handle(NS(msg_type=kind, seq=seq, payload=json.dumps(obj).encode(), received_ns=int(now[0] * 1e9)))
  client = NS(spec=NS(sha256=value['model']['sha256'], nbytes=value['model']['size']),
              t=NS(send_json=send), _next_seq=lambda: 3, _expect=lambda *a: replies.pop())
  watcher = comma.Watcher({'carrot_host': 'jetson', comma.CAPABILITY: True,
                           maintenance.CAPABILITY: True, 'carrot_source_commit': 'd' * 40},
                          NS(), clock=lambda: now[0], permission=NS(allowed=lambda: False))
  watcher.tick(client)  # Requesting a hold alone cannot retire the host.
  assert not actions
  now[0] += 1.1
  owner.held(external=False)
  owner.completed()
  with pytest.raises(startup.RuntimeHandoff):
    watcher.tick(client)
  assert actions == ['launch'] and session.maintenance_hold
  assert host.read_request(host.REQUEST_FILE, host.os.getuid())['internal']['token'] == owner.token
  host.enter()
  assert actions[1][:2] == ['systemctl', 'stop']
  assert actions[-1] == ('empty_gate', {'parked': True})
  assert not host.REQUEST_FILE.exists()
