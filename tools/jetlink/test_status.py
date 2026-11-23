import json
import subprocess

import pytest

from openpilot.common import jetlink_status as status


@pytest.mark.parametrize('active', [False, True])
def test_status_replaced_during_read_is_not_a_future_timestamp(monkeypatch, active):
  clock = [100.]
  monkeypatch.setattr(status.time, 'monotonic', lambda: clock[0])

  def concurrent_read(path):
    # The writer atomically replaces the report after the UI starts reading.
    clock[0] += .001
    if path == status.MODEL_STATUS:
      return {'updated': clock[0], 'active': active, 'warp_preparing': not active}
    return {'updated': clock[0], 'state': 'ready', 'peer': {'carrot_host': 'jetson'},
            'telemetry_updated': clock[0],
            'telemetry': {'carrot_health': {'age_s': 0., 'severity': 'ok'}}}

  monkeypatch.setattr(status, '_read', concurrent_read)
  expected = ('jetSON 사용 중', 'active') if active else ('jetSON 콤마 영상 준비 중', 'loading')
  assert status.badge() == expected
  assert status.diagnostics()['severity'] == 'ok'


@pytest.mark.parametrize('reason', [
  '[adb-handoff:opt_in_missing] ADB handoff permission is missing (/data/jetlink-adb-handoff)',
  "[adb-handoff:offroad] Offroad required: IsOffroad='0', IsOnroad='1' (pre-write recheck)",
  '[adb-handoff:io_error] unbind g1 UDC: PermissionError, errno=13',
])
def test_handoff_subprocess_error_reaches_screen_diagnostics(tmp_path, monkeypatch, reason):
  from openpilot.selfdrive.modeld.jetlink import mobile
  def rejected(command, **kwargs):
    raise subprocess.CalledProcessError(1, command, stderr=f'jetlink: {reason}\n')
  monkeypatch.setattr(mobile.subprocess, 'run', rejected)
  monkeypatch.setattr(mobile.os, 'geteuid', lambda: 0, raising=False)
  with pytest.raises(mobile.MobileLinkError) as error:
    mobile._root('setup_gadget.sh')
  link, model = tmp_path / 'link', tmp_path / 'model'
  monkeypatch.setattr(status, 'LINK_STATUS', link)
  monkeypatch.setattr(status, 'MODEL_STATUS', model)
  monkeypatch.setattr(status.time, 'monotonic', lambda: 10.)
  link.write_text(json.dumps({'updated': 10., 'state': 'retrying', 'error': str(error.value)[:300]}))
  diagnostic = status.diagnostics()
  assert diagnostic['severity'] == 'error'
  assert reason in diagnostic['reason']


@pytest.mark.parametrize(('peer', 'expected'), (
  ({'carrot_host': 'jetson'}, 'jetSON'), ({'carrot_host': 'mac'}, 'MAC'),
  ({'backend': 'trt', 'device': 'Orin-sm87'}, 'jetSON'),
  ({'protocol': 2, 'backend': 'ort', 'device': 'coreml-Apple-M1'}, 'MAC'),
  ({'protocol': 2, 'backend': 'ort', 'device': 'ane-Apple_M1_Pro'}, 'MAC'),
  ({'protocol': 3, 'backend': 'ort', 'device': 'ane-Apple_M4'}, 'MAC'),
  ({'protocol': 2, 'backend': 'ort', 'device': 'coreml-Apple_A17_Pro'}, 'Jetlink'),
  ({'backend': 'trt', 'device': 'RTX-sm89'}, 'Jetlink'),
  ({'carrot_host': 'untrusted text'}, 'Jetlink'), ({'carrot_host': []}, 'Jetlink'), (None, 'Jetlink'),
))
def test_host_identity_compatibility(peer, expected):
  assert status.host_label(peer) == expected


def test_phone_label_uses_explicit_transport_not_apple_m_chip_alone():
  ipad = {'protocol': 3, 'backend': 'ort', 'device': 'ane-whole-Apple_M4'}
  android = {'protocol': 3, 'backend': 'litert', 'device': 'gpu-Tensor_G4'}
  assert status.host_label(ipad, 'ios') == 'iOS'
  assert status.host_label(android, 'android') == 'Android'
  assert status.host_label(ipad) == 'MAC'
  assert status.host_label(android) == 'Jetlink'


def test_active_ready_and_expired_host_status(tmp_path, monkeypatch):
  link, model = tmp_path / 'link', tmp_path / 'model'
  monkeypatch.setattr(status, 'LINK_STATUS', link)
  monkeypatch.setattr(status, 'MODEL_STATUS', model)
  monkeypatch.setattr(status.time, 'monotonic', lambda: 10.)
  link.write_text(json.dumps({'updated': 9.9, 'state': 'ready', 'peer': {'carrot_host': 'mac'}}))
  assert status.badge() == ('MAC READY', 'ready')
  model.write_text(json.dumps({'updated': 9.9, 'active': True}))
  assert status.badge() == ('MAC', 'active')
  monkeypatch.setattr(status.time, 'monotonic', lambda: 14.)
  assert status.badge() == ('MAC ERROR', 'error')
  link.write_text('invalid')
  model.write_text('[]')
  assert status.badge() is None


@pytest.mark.parametrize(('phase', 'limit'), [('checking_usb', 125), ('transferring', 35), ('verifying', 125)])
def test_usb_update_status_stays_visible_until_ack_timeout(monkeypatch, phase, limit):
  update = {'state': phase, 'integrity': 'sha256', 'fraction': 0.5}
  link = {'updated': 100., 'state': 'updating', 'peer': {'carrot_host': 'jetson'}, 'host_update': update}
  monkeypatch.setattr(status.time, 'monotonic', lambda: 100. + limit - 1.)
  assert status.badge((link, {})) == status.update_badge(update)
  assert status.fresh_value(link, 104.) == {}
  assert status.badge(({**link, 'updated': 100. + limit - 1., 'state': 'retrying'}, {})) == ('jetSON ERROR', 'error')
  monkeypatch.setattr(status.time, 'monotonic', lambda: 100. + limit)
  assert status.badge((link, {})) == ('jetSON ERROR', 'error')


@pytest.mark.parametrize(('phase', 'label'), [('downloading', '다운로드'), ('transferring', 'USB 전송'),
                                             ('verifying', '파일 검증'), ('installing', '설치')])
def test_update_failure_phase_survives_reconnect_but_expires(monkeypatch, phase, label):
  entry = {'state': 'failed', 'failure_stage': phase, 'updated': 100., 'error': 'https://private.invalid/secret'}
  link = {'updated': 105., 'state': 'connecting', 'host_update_failure': entry}
  monkeypatch.setattr(status.time, 'monotonic', lambda: 105.)
  text = f'Jetson 업데이트 {label} 실패 · 재시도 대기'
  assert status.badge((link, {})) == (text, 'error')
  assert status.diagnostics((link, {}))['reason'] == text
  assert status.diagnostics((link, {}))['update'] is True
  link['updated'] = 135.
  monkeypatch.setattr(status.time, 'monotonic', lambda: 135.)
  assert status.badge((link, {})) != (text, 'error')


def test_unknown_failure_stage_never_exposes_remote_text():
  assert status.update_badge({'state': 'failed', 'failure_stage': ['private']}) == (
    'Jetson 업데이트 실패 · 재시도 대기', 'error')


def test_remote_badge_disappears_with_expired_snapshot(monkeypatch):
  import hud
  params = hud.DisplayParams()
  monkeypatch.setattr(hud, 'read_snapshot', lambda: (10., {'external_compute_label': 'jetSON'}))
  assert params.external_compute_label() == 'jetSON'
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  assert params.external_compute_label() == ''


def test_unplugged_optional_host_is_quiet_before_model_start_and_recovers_to_ready(tmp_path, monkeypatch):
  link, model = tmp_path / 'link', tmp_path / 'model'
  monkeypatch.setattr(status, 'LINK_STATUS', link)
  monkeypatch.setattr(status, 'MODEL_STATUS', model)
  monkeypatch.setattr(status.time, 'monotonic', lambda: 20.)
  link.write_text(json.dumps({'updated': 20., 'state': 'waiting', 'peer': {'carrot_host': 'jetson'}}))
  assert status.badge() is None
  assert status.diagnostics()['reason'] == 'Host not connected'
  for report in ({'updated': 20., 'active': False, 'error': ''},
                 {'updated': 10., 'active': False}):
    model.write_text(json.dumps(report))
    assert status.badge() is None
  for report in ({'updated': 20., 'active': False, 'error': 'inference timeout'},
                 {'updated': 20., 'active': True}):
    model.write_text(json.dumps(report))
    assert status.badge() == ('jetSON ERROR', 'error')
  model.unlink()
  link.write_text(json.dumps({'updated': 20., 'state': 'ready', 'peer': {'carrot_host': 'jetson'},
                             'telemetry_updated': 20., 'telemetry': {'carrot_health': {'age_s': 0., 'severity': 'ok'}}}))
  assert status.badge() == ('jetSON READY', 'ready')
  monkeypatch.setattr(status.time, 'monotonic', lambda: 24.)
  assert status.badge() == ('jetSON ERROR', 'error')
