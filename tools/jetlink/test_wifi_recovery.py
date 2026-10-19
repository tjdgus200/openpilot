"""Regression scenarios for retained AP sessions and prompt profile recovery."""
import hashlib
import json
import builtins
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import wifi_apply as host
import wifi_runtime
from test_wifi import config


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
@pytest.mark.parametrize('parked', [True, False])
@pytest.mark.parametrize('failure', [OSError, KeyError])
def test_one_power_query_failure_keeps_ap_then_recovers(monkeypatch, parked, failure):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'offroad', lambda: parked)
  ap = Mock()
  ap.active.return_value = True
  ap.needs_rechannel.return_value = False
  ap.awake.side_effect = [failure('synthetic query failure'), None]
  worker = direct.Supervisor(ap)
  worker.step()
  assert (worker.state, worker.reason) == ('active', 'power_policy_retry')
  worker.step()
  assert (worker.state, worker.reason) == ('active', '')
  ap.close.assert_not_called()
  ap.start.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_nm_query_failure_never_deletes_even_an_unverified_ap(monkeypatch):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'offroad', lambda: False)
  ap = Mock()
  ap.active.side_effect = RuntimeError('synthetic NM failure')
  worker = direct.Supervisor(ap)
  worker.step()
  assert (worker.state, worker.reason) == ('degraded', 'ap_query_failed')
  ap.close.assert_not_called()
  ap.start.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_unsupported_phone_channel_preserves_working_ap(monkeypatch):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'offroad', lambda: True)
  ap = Mock()
  ap.active.return_value = True
  ap.needs_rechannel.side_effect = direct.ChannelUnavailable('station_channel_unsupported')
  worker = direct.Supervisor(ap)
  worker.step()
  assert (worker.state, worker.reason) == ('active', 'station_channel_unsupported')
  ap.close.assert_not_called()
  ap.start.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_confirmed_outage_is_not_reconfigured_onroad(monkeypatch):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'offroad', lambda: False)
  ap = Mock()
  ap.active.return_value = False
  worker = direct.Supervisor(ap)
  worker.step()
  assert worker.state == 'waiting_offroad'
  ap.close.assert_not_called()
  ap.start.assert_not_called()
  monkeypatch.setattr(direct, 'offroad', lambda: True)
  worker.step()
  assert worker.state == 'active'
  ap.start.assert_called_once()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_failed_initial_activation_cleans_only_partial_ap(monkeypatch):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'offroad', lambda: True)
  ap = Mock()
  ap.active.return_value = False
  ap.start.side_effect = direct.ChannelUnavailable('station_channel_unsupported')
  worker = direct.Supervisor(ap)
  assert worker.step() == 30.
  assert (worker.state, worker.reason) == ('unavailable', 'station_channel_unsupported')
  assert ap.close.call_count == 2


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_compatible_update_defers_onroad_handover_and_official_return_still_retires(tmp_path):
  import wifi_direct as direct
  source = tmp_path / 'wifi_direct.py'
  policy = tmp_path / 'wifi_direct_policy.json'
  source.write_text('old')
  digest = hashlib.sha256(source.read_bytes()).digest()
  policy.write_text('{"format":1,"direct_only":true}')
  assert direct.source_action(source, digest, False) == 'continue'
  source.write_text('new')
  assert direct.source_action(source, digest, False) == 'defer'
  assert direct.source_action(source, digest, True) == 'handover'
  policy.unlink()
  assert direct.source_action(source, digest, False) == 'stop'
  policy.write_text('{"format":999,"direct_only":true}')
  assert direct.source_action(source, digest, True) == 'stop'


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_new_controller_adopts_existing_owned_ap_without_bouncing_link(monkeypatch):
  import wifi_direct as direct
  value = {'uuid':'synthetic-ap'}
  monkeypatch.setattr(direct, 'configuration', lambda: value)
  wireless = Mock()
  wireless.interfaces.return_value = [{'name':'wljetson0','index':41,'type':3}]
  def command(*args):
    if args[-1] == '--active': return 'synthetic-ap'
    return {'connection.interface-name':'wljetson0','802-11-wireless.mode':'ap',
            '802-11-wireless.band':'a','802-11-wireless.channel':'149'}[args[1]]
  ap = direct.AccessPoint(wireless, command)
  assert ap.adopt()
  assert (ap.index, ap.value, ap.band_channel) == (41, value, ('a',149))
  wireless.create.assert_not_called()
  wireless.delete.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_onroad_transition_during_preparation_does_not_activate_ap(tmp_path, monkeypatch):
  import wifi_direct as direct
  states = iter([True, True, False])
  monkeypatch.setattr(direct, 'offroad', lambda: next(states))
  monkeypatch.setattr(direct, 'configuration', lambda: dict(uuid='synthetic',subnet='172.29.77.0/24',ssid='synthetic',password='synthetic'))
  monkeypatch.setattr(direct, 'channel', lambda: ('a',36))
  monkeypatch.setattr(direct.subprocess, 'check_output', lambda *a, **k: '[]')
  monkeypatch.setattr(direct, 'PROFILE', tmp_path/'ap.nmconnection')
  wireless = Mock(); wireless.create.return_value = 41
  command = Mock(return_value='30 (disconnected)')
  ap = direct.AccessPoint(wireless, command)
  assert not ap.start()
  assert not any('up' in c.args for c in command.call_args_list)


def test_new_empty_profile_reports_only_allowlisted_ap_diagnostic(tmp_path):
  value = config(); value['profiles'] = []
  value['direct_ap_reason'] = 'comma_ap_waiting_offroad'
  worker = host.Worker(tmp_path, lambda *a: 'wlan0:wifi:disconnected' if a[-1]=='status' else '')
  assert worker.step((0.,value),0.) == 'waiting_for_comma_wifi'
  assert worker.reason == 'comma_ap_waiting_offroad'
  value['direct_ap_reason'] = 'synthetic secret must never be copied'
  worker.step((30.,value),30.)
  assert worker.reason == ''


def test_new_profile_bypasses_retry_but_repeated_failed_packet_does_not(tmp_path):
  calls = []
  def command(*args):
    calls.append(args)
    if args[-1] == 'status': return 'wlan0:wifi:disconnected'
    if args[0] == '--wait': raise RuntimeError('synthetic activation failure')
    return ''
  worker = host.Worker(tmp_path, command)
  assert worker.step(None, 0.) == 'waiting_for_comma_wifi'
  assert worker.step((1., config()), 1.) == 'retrying'
  count = len(calls)
  worker.step((2., config()), 2.)
  assert len(calls) == count
  worker.step((3., config(password='new synthetic password')), 3.)
  assert len(calls) > count
  assert worker.reason == 'connect_failed'


def test_same_profiles_with_new_road_state_do_not_bypass_backoff(tmp_path):
  worker = host.Worker(tmp_path, Mock(side_effect=RuntimeError('synthetic failure')))
  value = config()
  worker.step((0., value), 0.)
  count = worker.command.call_count
  value = config(); value['onroad'] = True
  worker.step((1., value), 1.)
  assert worker.command.call_count == count


def test_ap_generation_wakes_refresh_without_exporting_names(monkeypatch):
  import wifi_publisher as sender
  value = {'state':'active','index':41,'updated':99.,'reason':'station_channel_unsupported',
           'untrusted_extra':'synthetic private value'}
  monkeypatch.setattr(Path, 'read_text', lambda *a, **k: json.dumps(value))
  assert sender.ap_generation(100.) == 41
  assert sender.ap_diagnostic(100.) == 'comma_ap_channel_unavailable'
  assert sender.ap_generation(120.) is None
  assert sender.ap_diagnostic(120.) == ''
  value.update(state='active',reason='synthetic private value')
  assert sender.ap_diagnostic(100.) == ''


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux AP supervisor uses fcntl')
def test_adoption_never_touches_foreign_profile_interface(monkeypatch):
  import wifi_direct as direct
  monkeypatch.setattr(direct, 'configuration', lambda: {'uuid':'synthetic'})
  wireless = Mock()
  wireless.interfaces.return_value = [{'name':'wljetson0','index':41,'type':3}]
  ap = direct.AccessPoint(wireless, Mock(return_value='wlan0'))
  with pytest.raises(ValueError): ap.adopt()
  assert ap.index is None and ap.value is None
  wireless.delete.assert_not_called()


@pytest.mark.skipif(sys.platform == 'win32', reason='Real Linux control lock')
@pytest.mark.parametrize('action, closed', [('handover',False),('stop',True)])
@pytest.mark.parametrize('power_failure', [False, True])
def test_supervisor_exit_preserves_only_compatible_update_ap(tmp_path, monkeypatch, action, closed, power_failure):
  import wifi_direct as direct
  monkeypatch.setattr(direct.os,'geteuid',lambda:0)
  monkeypatch.setattr(direct,'low_priority',lambda:None)
  defaults = Mock(side_effect=RuntimeError('synthetic default-policy failure') if power_failure else None)
  monkeypatch.setattr(direct,'configure_power',defaults)
  monkeypatch.setattr(direct.signal,'signal',lambda *a:None)
  monkeypatch.setattr(direct,'offroad',lambda:True)
  monkeypatch.setattr(direct,'open',lambda *a,**k:builtins.open(tmp_path/'control.lock','a'),raising=False)
  monkeypatch.setattr(direct,'STATUS',tmp_path/'status.json')
  steps=iter(['continue',action])
  monkeypatch.setattr(direct,'source_action',lambda *a:next(steps))
  event=Mock(); event.is_set.return_value=False
  monkeypatch.setattr(direct.threading,'Event',lambda:event)
  wireless=Mock()
  monkeypatch.setattr(direct,'Wireless',lambda:wireless)
  ap=Mock(index=41,stage='active')
  ap.active.return_value=True
  ap.needs_rechannel.return_value=False
  monkeypatch.setattr(direct,'AccessPoint',lambda _:ap)
  direct.serve()
  assert ap.close.called is closed
  ap.start.assert_not_called()
  defaults.assert_called_once()
  wireless.close.assert_called_once()


def test_real_ram_helpers_import_without_current_release_or_site_packages(tmp_path):
  wifi_runtime.configure(tmp_path,Path(__file__).parent,lambda *a,**k:None)
  directory=next((tmp_path/'run/carrot-jetlink-network').iterdir())
  code="import sys; sys.path.insert(0,sys.argv[1]); import wifi_apply; assert hasattr(wifi_apply.Worker(),'attempted')"
  subprocess.run([sys.executable,'-I','-B','-c',code,str(directory)],check=True,timeout=10)


def test_runtime_migration_uses_independent_ram_copy_and_is_idempotent(tmp_path):
  source = tmp_path / 'signed-source'
  source.mkdir()
  for name in wifi_runtime.NAMES:
    (source / name).write_text('# synthetic approved helper\n')
  commands = []
  command = lambda args, **kwargs: commands.append(args)
  assert wifi_runtime.configure(tmp_path, source, command)
  dropin = tmp_path/'run/systemd/system/carrot-jetlink-wifi.service.d/runtime.conf'
  body = dropin.read_text()
  assert '/run/carrot-jetlink-network/' in body and '/current/' not in body
  assert len(list((tmp_path/'run/carrot-jetlink-network').glob('*/wifi_apply.py'))) == 1
  assert not (tmp_path/'usr/lib/carrot-jetlink-storage').exists()
  assert commands == [['systemctl','daemon-reload'], ['systemctl','try-restart','carrot-jetlink-wifi']]
  assert not wifi_runtime.configure(tmp_path, source, command)
  assert len(commands) == 2


def test_runtime_migration_waits_for_profile_transaction(tmp_path):
  source = tmp_path/'source'; source.mkdir()
  for name in wifi_runtime.NAMES: (source/name).write_text('# synthetic\n')
  journal = tmp_path/'etc/NetworkManager/system-connections/.carrot-wifi-rollback.json'
  journal.parent.mkdir(parents=True)
  journal.write_text('{}')
  command = Mock()
  with pytest.raises(RuntimeError): wifi_runtime.configure(tmp_path, source, command)
  command.assert_not_called()
  assert not (tmp_path/'run/systemd/system/carrot-jetlink-wifi.service.d/runtime.conf').exists()


def test_runtime_migration_failure_retains_immutable_configuration(tmp_path):
  source = tmp_path/'source'; source.mkdir()
  for name in wifi_runtime.NAMES: (source/name).write_text('# synthetic\n')
  with pytest.raises(RuntimeError):
    wifi_runtime.configure(tmp_path, source, Mock(side_effect=RuntimeError('synthetic failure')))
  assert not (tmp_path/'run/systemd/system/carrot-jetlink-wifi.service.d/runtime.conf').exists()
