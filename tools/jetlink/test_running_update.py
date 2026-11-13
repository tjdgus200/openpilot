"""Comma-only restart with a resident old Jetson; no device or live services."""
from functools import partial
import json
from types import SimpleNamespace as NS

import pytest

from openpilot.common import jetlink_maintenance as maintenance
from openpilot.common import jetson_model
from openpilot.selfdrive.modeld.jetlink import park_update as comma, startup
from openpilot.selfdrive.modeld.jetlink.contracts import JETSON_SPEC
from test_internal_update import ownership  # noqa: F401 -- boot-local isolated owner/lease
from test_update_host import manifest


@pytest.fixture
def resident(ownership, monkeypatch):  # noqa: F811
  owner, lease, now = ownership
  value = manifest()
  value.update(delivery='usb', profile='jetson-pv5-test')
  value['model'].update(sha256=JETSON_SPEC.sha256, size=JETSON_SPEC.nbytes)
  peer = {'carrot_host': 'jetson', comma.CAPABILITY: True, maintenance.CAPABILITY: True,
          'carrot_source_commit': 'd' * 40, 'loaded': JETSON_SPEC.sha256}
  monkeypatch.setattr(jetson_model, 'selected_release', lambda *a, **kw: value)
  monkeypatch.setattr(comma, 'Watcher', partial(comma.Watcher, clock=lambda: now[0]))
  monkeypatch.setattr(comma.ParkPermission, 'allowed', lambda _: False)
  calls, reports = [], []

  def send(kind, seq, payload):
    assert lease.allowed()  # Only a completed local output can retire this server.
    calls.append((kind, payload))

  client = NS(spec=None, t=NS(send_json=send), _next_seq=lambda: len(calls) + 1,
              _expect=lambda *a: NS(payload=b'{"accepted":true}'),
              state=lambda: calls.append('state'))

  def sleep(delay):
    now[0] += delay
    assert owner.held(external=False)
    owner.completed()

  run = partial(comma.wait_for_running_update, client, peer, NS(), lambda: True,
                lambda *a, **kw: reports.append(kw), sleep=sleep)
  return NS(owner=owner, lease=lease, now=now, value=value, peer=peer, client=client,
            calls=calls, reports=reports, run=run)


def test_comma_only_reboot_retires_resident_host_before_any_engine_request(resident):
  r = resident
  # A resident runtime does NOT advertise the empty boot gate capability.
  startup.wait_for_boot_update(r.client, r.peer, None, lambda: True, lambda *a, **kw: None)
  assert r.calls == []
  with pytest.raises(startup.RuntimeHandoff):
    r.run()
  assert r.client.spec is None  # No ENGINE_REQ, contract parsing or probe inference.
  request = next(row for row in r.calls if isinstance(row, tuple))
  assert request == (comma.REQUEST, {'manifest': r.value, 'internal': r.lease.proof})
  assert r.reports and r.owner.token == r.lease.token


@pytest.mark.parametrize('key', ['n16', 'flip-flop', 'ctv3m', 'light-speed', 'light-speed-2'])
def test_all_five_exact_model_pins_reconcile_without_preparing_old_engine(resident, monkeypatch, key):
  from jetlink.spec import ModelSpec
  from openpilot.common import big_model_catalog
  from openpilot.selfdrive.modeld.jetlink import contracts
  r = resident
  spec = ModelSpec.from_dict(big_model_catalog.model_spec(key))
  monkeypatch.setattr(contracts, 'JETSON_SPEC', spec)
  r.value.clear()
  r.value.update(big_model_catalog.usb_release(key))
  r.peer['loaded'] = spec.sha256
  with pytest.raises(startup.RuntimeHandoff):
    r.run()
  request = next(row[1] for row in r.calls if isinstance(row, tuple))
  assert request['manifest'] == big_model_catalog.usb_release(key)
  assert request['manifest']['model']['sha256'] == spec.sha256
  assert request['manifest']['model']['size'] == spec.nbytes
  assert r.client.spec is None


@pytest.mark.parametrize('case', ['same', 'mac', 'unsupported', 'non_pv5', 'different_model',
                                'different_size', 'local', 'malformed'])
def test_no_startup_change_without_eligible_pv5_runtime_difference(resident, case):
  r = resident
  if case == 'same': r.peer['carrot_source_commit'] = r.value['source_commit']
  elif case == 'mac': r.peer['carrot_host'] = 'mac'
  elif case == 'unsupported': r.peer[comma.CAPABILITY] = False
  elif case == 'non_pv5': r.value['profile'] = 'ordinary-host'
  elif case == 'different_model': r.value['model']['sha256'] = 'f' * 64
  elif case == 'different_size': r.value['model']['size'] += 1
  elif case == 'local': r.value['delivery'] = 'local'
  elif case == 'malformed': del r.value['model']
  r.run()
  assert not r.calls and not r.reports and r.owner.token is None


@pytest.mark.parametrize('proof_state', ['no_output', 'external_active', 'stale_output'])
def test_unproven_native_ownership_never_sends_update_or_starts_engine(resident, proof_state):
  r = resident
  connected = [True]

  def sleep(delay):
    r.now[0] += delay
    if proof_state != 'external_active':
      assert r.owner.held(external=False)
    else:
      assert not r.owner.held(external=True)
    if proof_state == 'stale_output':
      r.owner.completed()
      r.now[0] += .26
    if r.now[0] > 102.:
      connected[0] = False

  with pytest.raises(ConnectionError, match='disconnected'):
    comma.wait_for_running_update(r.client, r.peer, NS(), lambda: connected[0],
                                  lambda *a, **kw: None, sleep=sleep)
  assert not any(isinstance(row, tuple) for row in r.calls)
  assert r.client.spec is None


def test_rejected_recent_host_inference_retries_without_new_probe_inference(resident):
  r = resident
  attempts = []

  def response(*a):
    attempts.append(r.now[0])
    return NS(payload=json.dumps({'accepted': len(attempts) > 1}).encode())

  r.client._expect = response
  with pytest.raises(startup.RuntimeHandoff):
    r.run()
  assert len(attempts) == 2 and attempts[1] - attempts[0] >= 5.
  assert r.client.spec is None and r.owner.token == r.lease.token


def test_legacy_park_handoff_does_not_require_internal_capability(resident, monkeypatch):
  r = resident
  r.peer[maintenance.CAPABILITY] = False
  monkeypatch.setattr(comma.ParkPermission, 'allowed', lambda _: True)
  r.client.t.send_json = lambda kind, seq, payload: r.calls.append(payload)
  with pytest.raises(startup.RuntimeHandoff):
    r.run()
  assert r.calls == [{'manifest': r.value, 'parked': True}]
  assert r.owner.token is None


def test_pending_handoff_keeps_existing_wifi_provisioning_alive(resident):
  r = resident
  provisioned = []
  with pytest.raises(startup.RuntimeHandoff):
    r.run(wifi=NS(send=lambda client: provisioned.append(client)))
  assert provisioned and all(client is r.client for client in provisioned)
