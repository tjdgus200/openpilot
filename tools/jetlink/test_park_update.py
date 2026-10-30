"""PC fault injection only: no real systemd, USB, model probe or vehicle."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import park_update as host
import boot_update as boot
import update_host as update
from openpilot.selfdrive.modeld.jetlink import park_update as comma, startup
from test_update_host import manifest


class Sample:
  def __init__(self):
    self.valid = dict.fromkeys(('carState', 'carControl', 'selfdriveState'), True)
    self.alive = dict(self.valid)
    self.recv_time = dict.fromkeys(self.valid, 100.)
    self.data = {'carState': NS(canValid=True, canTimeout=False, gearShifter='park', vEgoRaw=0.),
                 'carControl': NS(enabled=False), 'selfdriveState': NS(active=False)}

  def __getitem__(self, key):
    return self.data[key]

  def update(self, timeout):
    assert timeout == 0


@pytest.mark.parametrize('bad', ['drive', 'reverse', 'speed', 'nan', 'bool', 'can', 'timeout',
    'enabled', 'active', 'missing', 'stale_car', 'stale_control', 'stale_state', 'future', 'invalid', 'dead'])
def test_handoff_requires_fresh_park_zero_and_inactive(bad):
  sm = Sample()
  if bad in ('drive', 'reverse'):
    sm['carState'].gearShifter = bad
  elif bad in ('speed', 'nan', 'bool'):
    sm['carState'].vEgoRaw = {'speed': .001, 'nan': float('nan'), 'bool': False}[bad]
  elif bad == 'can':
    sm['carState'].canValid = False
  elif bad == 'timeout':
    sm['carState'].canTimeout = True
  elif bad == 'enabled':
    sm['carControl'].enabled = True
  elif bad == 'active':
    sm['selfdriveState'].active = True
  elif bad == 'missing':
    del sm.data['carControl']
  elif bad.startswith('stale_'):
    sm.recv_time[{'stale_car': 'carState', 'stale_control': 'carControl', 'stale_state': 'selfdriveState'}[bad]] = 99.
  elif bad == 'future':
    sm.recv_time['carControl'] = 101.
  elif bad == 'invalid':
    sm.valid['selfdriveState'] = False
  elif bad == 'dead':
    sm.alive['carState'] = False
  guard = comma.ParkPermission(NS(get_bool=lambda k: k == 'IsOnroad'), factory=lambda: sm, clock=lambda: 100.1)
  assert not guard.allowed()


@pytest.mark.parametrize('offroad,onroad,expected', [(False, True, True), (True, False, False),
    (True, True, False), (False, False, False)])
def test_only_unambiguous_onroad_park_requests_running_runtime_handoff(offroad, onroad, expected):
  guard = comma.ParkPermission(NS(get_bool=lambda k: {'IsOnroad': onroad, 'IsOffroad': offroad}[k]),
      factory=Sample, clock=lambda: 100.1)
  assert guard.allowed() == expected


def setup_watcher(monkeypatch, *, supported=True, parked=True):
  value = manifest()
  value['delivery'] = 'usb'
  calls = []
  monkeypatch.setattr(comma, 'time', NS(monotonic=lambda: 100.))
  monkeypatch.setattr(__import__('openpilot.common.jetson_model', fromlist=['selected_release']),
                      'selected_release', lambda *a, **k: value)
  client = NS(spec=NS(sha256=value['model']['sha256'], nbytes=value['model']['size']), _next_seq=lambda: 3,
      t=NS(send_json=lambda *a: calls.append(a)),
      _expect=lambda kind, seq, timeout: NS(payload=b'{"accepted":true}'))
  watcher = comma.Watcher({'carrot_host': 'jetson', comma.CAPABILITY: supported,
      'carrot_source_commit': 'd' * 40}, NS(), clock=lambda: 100., permission=NS(allowed=lambda: parked))
  return watcher, client, value, calls


def test_changed_pin_causes_single_controlled_handoff(monkeypatch):
  watcher, client, value, calls = setup_watcher(monkeypatch)
  with pytest.raises(startup.RuntimeHandoff):
    watcher.tick(client)
  assert calls == [(host.REQUEST, 3, {'manifest': value, 'parked': True})]


@pytest.mark.parametrize('case', ['legacy', 'drive', 'same', 'other_model', 'other_size', 'local', 'broken'])
def test_no_interruption_or_upload_without_eligible_changed_pin(monkeypatch, case):
  watcher, client, value, calls = setup_watcher(monkeypatch, supported=case != 'legacy', parked=case != 'drive')
  if case == 'same':
    value['source_commit'] = 'd' * 40
  if case == 'other_model':
    client.spec.sha256 = 'f' * 64
  if case == 'other_size':
    client.spec.nbytes += 1
  if case == 'local':
    value['delivery'] = 'local'
  if case == 'broken':
    value.pop('model')
  watcher.tick(client)
  assert calls == []


def test_denied_host_request_does_not_spin_or_stop_model(monkeypatch):
  watcher, client, _, calls = setup_watcher(monkeypatch)
  client._expect = lambda *a: NS(payload=b'{"accepted":false}')
  watcher.tick(client)
  watcher.next_poll = 0
  watcher.tick(client)
  assert len(calls) == 1


@pytest.mark.parametrize('received', [98., 101., float('nan')])
def test_host_rejects_stale_future_or_nan_request_before_file_write(monkeypatch, received):
  monkeypatch.setattr(host, 'supported', lambda: True)
  with pytest.raises(ValueError):
    host.prepare(b'{}', received, Path('/nonexistent'), clock=lambda: 100.)


def test_host_validates_unregistered_pin_without_allowing_model_switch(tmp_path, monkeypatch):
  import usb_release
  import openpilot.common.jetson_model as model
  value = manifest()
  value['delivery'] = 'usb'
  (tmp_path / 'current').mkdir()
  (tmp_path / 'current/SOURCE_COMMIT').write_text('d' * 40)
  calls = []
  monkeypatch.setattr(usb_release, 'trusted', lambda root, v: calls.append(v))
  monkeypatch.setattr(update, 'validate_storage_compatibility', lambda _: None)
  monkeypatch.setattr(update, 'selected_commit', lambda _: pytest.fail('Not yet uploaded/registered'))
  monkeypatch.setattr(model, 'read_spec', lambda _: value['model'])
  assert host.validate_request({'manifest': value, 'parked': True}, tmp_path) == value
  assert calls == [value]
  monkeypatch.setattr(model, 'read_spec', lambda _: {'sha256': 'e' * 64, 'size': 3})
  with pytest.raises(ValueError, match='boot-selected'):
    host.validate_request({'manifest': value, 'parked': True}, tmp_path)


def test_short_permission_lease_and_explicit_revoke(monkeypatch):
  now = [100.]
  monkeypatch.setattr(boot.time, 'monotonic', lambda: now[0])
  gate = boot.Gate(require_permission=True)
  gate.selected, gate.received = {}, 100.
  assert gate.allowed()
  now[0] = 101.
  assert not gate.allowed()
  gate.received = 100.8
  gate_session = boot.BootstrapSession(NS(), gate)
  gate_session.handle(NS(msg_type=boot.MESSAGE, seq=1, payload=b'{"pause":true}'))
  assert not gate.allowed()








def test_launch_has_only_fixed_local_actions(monkeypatch):
  calls = []
  monkeypatch.setattr(host.subprocess, 'run', lambda cmd, **kw: calls.append((cmd, kw)))
  host.launch(Path('/opt/carrot-jetlink'))
  command, options = calls[0]
  assert command[:3] == ['sudo', '-n', 'systemd-run'] and command[-1] == 'parked-boot'
  assert '--property=CPUAffinity=0 1' in command and options['timeout'] == 10
  assert not options.get('shell') and not any('reboot' in v for v in command)


def test_running_server_acknowledges_before_scheduling_retirement(monkeypatch):
  import server
  calls = []
  monkeypatch.setattr(host, 'prepare', lambda *a: calls.append('validate'))
  monkeypatch.setattr(host, 'launch', lambda *a: calls.append('launch'))
  session = server.CarrotSession.__new__(server.CarrotSession)
  session.last_seq = 0
  session._send_json = lambda kind, seq, value: calls.append(('ack', kind, value))
  session.handle(NS(msg_type=host.REQUEST, seq=1, payload=b'{}', received_ns=100))
  assert calls == ['validate', ('ack', host.RESPONSE, {'accepted': True}), 'launch']
  session.handle(NS(msg_type=host.REQUEST, seq=1, payload=b'{}', received_ns=100))
  assert len(calls) == 3  # Replay cannot start another worker.


@pytest.mark.parametrize('failure', [ValueError(), OSError()])
def test_rejected_handoff_does_not_stop_server(monkeypatch, failure):
  import server
  def reject(*a): raise failure
  monkeypatch.setattr(host, 'prepare', reject)
  monkeypatch.setattr(host, 'launch', lambda *a: pytest.fail('Invalid request scheduled'))
  session = server.CarrotSession.__new__(server.CarrotSession)
  session.last_seq = 0
  replies = []
  session._send_json = lambda kind, seq, value: replies.append(value)
  session.handle(NS(msg_type=host.REQUEST, seq=1, payload=b'{}', received_ns=100))
  assert replies == [{'accepted': False}]


@pytest.mark.parametrize('expire', [True, False])
def test_probe_retires_all_children_on_permission_loss(monkeypatch, expire):
  calls = []
  class Process:
    pid = 12345
    def wait(self, timeout=None):
      calls.append(('wait', timeout))
      return 0
  monkeypatch.setattr(update.subprocess, 'Popen', lambda *a, **k: Process())
  monkeypatch.setattr(update.os, 'killpg', lambda pid, sig: calls.append(('kill', pid)), raising=False)
  monkeypatch.setattr(update.signal, 'SIGKILL', 9, raising=False)
  if expire:
    with pytest.raises(update.ActivationDeferred):
      update.probe_release(Path('/synthetic'), allowed=lambda: False)
    assert calls == [('kill', 12345), ('wait', None)]
  else:
    update.probe_release(Path('/synthetic'), allowed=lambda: True)
    assert calls == [('wait', .2)]


@pytest.mark.parametrize('bad', ['', 'stale', 'future', 'drive', 'moving', 'enabled', 'active',
                                'invalid', 'wrong_event', 'malformed'])
def test_host_independently_checks_real_decoded_vehicle_state(monkeypatch, bad):
  import base64
  import hud_protocol
  from openpilot.cereal import log
  state = {'canValid': True, 'canTimeout': False, 'gearShifter': 'drive' if bad == 'drive' else 'park',
           'vEgoRaw': .1 if bad == 'moving' else 0.}
  events = {name: base64.b64encode(log.Event.new_message(**{name: fields}).to_bytes()).decode()
      for name, fields in (('carState', state), ('carControl', {'enabled': bad == 'enabled'}),
                           ('selfdriveState', {'active': bad == 'active'}))}
  if bad == 'wrong_event':
    events['carState'] = events['carControl']
  if bad == 'malformed':
    events['carState'] = 'invalid%'
  value = {'sent': 100., 'received': dict.fromkeys(events, 100.),
           'valid': dict.fromkeys(events, bad != 'invalid'), 'alive': dict.fromkeys(events, True), 'events': events}
  stamp = 99. if bad == 'stale' else 101. if bad == 'future' else 100.
  monkeypatch.setattr(hud_protocol, 'read_snapshot', lambda: (stamp, value))
  monkeypatch.setattr(host.time, 'monotonic', lambda: 100.1)
  assert host.runtime_parked() == (bad == '')


def test_parked_gate_revokes_even_when_selected_package_is_already_registered(monkeypatch):
  from tools.jetlink import usb_release as usb, usb_release_client as upload  # noqa: TID251 -- shared protocol
  value = manifest()
  value['delivery'] = 'usb'
  monkeypatch.setattr(startup, 'selected_release', lambda *a, **k: value)
  monkeypatch.setitem(sys.modules, 'openpilot.common.params', NS(Params=lambda: NS()))
  permissions = iter((False, False, True))
  monkeypatch.setattr(comma, 'GatePermission', lambda *a, **k: NS(allowed=lambda: next(permissions)))
  monkeypatch.setattr(upload, 'available', lambda *a: True)
  monkeypatch.setattr(upload, 'upload', lambda *a, **k: pytest.fail('Already registered'))
  sent, states = [], []
  client = NS(t=NS(send_json=lambda kind, seq, val: sent.append(val)), _next_seq=lambda: 1,
      state=lambda: {'carrot_update': {'state': 'ready'}})
  peer = {'carrot_host': 'jetson', startup.CAPABILITY: True, 'carrot_park_update_gate_v1': True,
      usb.SHA256_CAPABILITY: True, usb.CHECK_CAPABILITY: True, 'usb_source_commit': value['source_commit']}
  with pytest.raises(startup.RuntimeHandoff):
    startup.wait_for_boot_update(client, peer, None, lambda: True,
        lambda *a, **k: states.append(k['host_update']['state']), sleep=lambda _: None)
  assert sent == [{'pause': True}, {'pause': True}, value]
  assert states == ['waiting_park', 'waiting_park', 'ready']
