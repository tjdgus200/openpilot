import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boot_display import lines, render, panel_frame
import boot_status


@pytest.mark.parametrize(('sha', 'label'), [
  ('404a18cfd86d29637d20c697dfde245bb47c666ae016730ab674c65f4d1e1aa4', 'CTV3M'),
  ('35976b11be3904eb78e32f7dc55a6abf8e9433ac5aa391a1998f1e9ce001b199', 'Flip Flop'),
  ('01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a', 'N16 Nav'),
  ('404a18cf', '--'), (None, '--'), ({}, '--'),
])
def test_boot_model_names_require_exact_known_sha(sha, label):
  assert boot_status.model_name(sha) == label


def test_offline_diagnosis_without_comma_model_or_xorg(tmp_path):
  title, rows, detail, error = lines({'stage': {'stage': 'root-readonly-check', 'error': 'RuntimeError'},
                                    'units': {'carrot-protected-storage': {'ActiveState': 'failed'}}})
  assert error and title[1] == 'BOOT NEEDS ATTENTION'
  assert rows[3][2] == 'Waiting for network'
  assert 'carrot-protected-storage' in detail
  # The frame is CPU-only and fully renderable without raylib/TensorRT/Xorg.
  frame = render({'stage': {'stage': 'root-readonly-check', 'error': 'RuntimeError'},
                  'uptime': 25})
  assert frame.size == (1920, 462)
  frame.save(tmp_path / 'boot-error.png')


def test_active_service_does_not_claim_model_ready():
  title, rows, _, _ = lines({'units': {'carrot-jetlink': {'ActiveState': 'active'}}})
  assert 'WAITING' in title[1]
  assert rows[5] == ('콤마 USB 서비스', 'COMMA USB SERVICE', 'active')


@pytest.mark.parametrize('state', ['checking', 'transferring', 'verifying', 'ready', 'failed'])
def test_boot_update_phase_progress_and_previous_release_visible(state):
  title, rows, detail, error = lines({'update': {'state': state, 'fraction': .5,
      'reason': 'validation_failed' if state == 'failed' else ''}, 'model': 'Flip Flop',
      'storage_free': '150 / 200 GiB'})
  assert len(rows) == 9 and '50%' in rows[6][2]
  assert rows[7][2] == 'Flip Flop' and rows[8][2] == '150 / 200 GiB'
  assert error == (state == 'failed')
  if state == 'failed':
    assert '이전 버전 보존' in detail
  if state == 'ready':
    assert '모델 준비 대기' in rows[6][2]  # USB gate readiness is not inference readiness.
  assert render({'update': {'state': state, 'fraction': .5}, 'model': 'Flip Flop'}).size == (1920, 462)


@pytest.mark.parametrize('stamp,expected', [(99., True), (90., False), (101., False), ('now', False)])
def test_boot_update_status_freshness_and_private_error_filter(monkeypatch, stamp, expected):
  monkeypatch.setattr(boot_status, 'read_json', lambda _: {'updated': stamp, 'state': 'failed',
      'fraction': .4, 'error': 'Candidate validation failed; private-value-must-not-pass'})
  value = boot_status.update_status(100.)
  assert bool(value) == expected
  if expected:
    assert value == {'state': 'failed', 'fraction': .4, 'reason': 'validation_failed'}
    assert 'private' not in json.dumps(value)


def test_runtime_diagnostic_refreshes_on_release_change_once(tmp_path, monkeypatch):
  import install_updates as install
  current = tmp_path / 'current'
  current.mkdir()
  (current / 'SOURCE_COMMIT').write_text('a' * 40)
  target = tmp_path / 'run/runtime.conf'
  monkeypatch.setattr(install, 'BOOT_DISPLAY_DROPIN', target)
  calls = []
  monkeypatch.setattr(install.subprocess, 'run', lambda args, **kw: calls.append(args))
  install.configure_runtime_boot_display(tmp_path)
  assert 'CARROT_BOOT_DISPLAY_SOURCE=' + 'a' * 40 in target.read_text()
  calls.clear()
  install.migrate_boot_display(tmp_path)
  assert calls == []
  (current / 'SOURCE_COMMIT').write_text('b' * 40)
  install.migrate_boot_display(tmp_path)
  assert len(calls) == 1 and calls[0][-1] == '--boot-display-runtime'


def test_optional_diagnostic_failure_never_blocks_runtime_migration(tmp_path, monkeypatch):
  import install_updates as install
  original = Path.is_file
  monkeypatch.setattr(Path, 'is_file', lambda p:
      p.name in ('carrot-jetlink-boot-display.service', 'boot_update.py', 'boot-update-required') or original(p))
  def reject(_):
    raise subprocess.CalledProcessError(1, 'systemctl')
  monkeypatch.setattr(install, 'migrate_boot_display', reject)
  install.migrate_running_release(tmp_path)  # No exception; model server may continue.


def test_unreadable_thermal_sensor_does_not_hide_boot_diagnosis(monkeypatch):
  original = Path.read_text

  def read(self, *args, **kwargs):
    if self.as_posix().endswith('/thermal_zone0/temp'):
      # Observed on Jetson Python3.10 when a sysfs read returns EAGAIN.
      raise TypeError("can't concat NoneType to bytes")
    if self.as_posix().endswith('/thermal_zone1/temp'):
      return '55000'
    return original(self, *args, **kwargs)

  monkeypatch.setattr(Path, 'read_text', read)
  monkeypatch.setattr(Path, 'glob', lambda self, pattern: iter([
    Path('/sys/class/thermal/thermal_zone0/temp'),
    Path('/sys/class/thermal/thermal_zone1/temp')]))
  monkeypatch.setattr(boot_status.subprocess, 'run', lambda *a, **k:
                      subprocess.CompletedProcess(a, 0, stdout='[]', stderr=''))
  status = boot_status.collect()
  assert status['temp_c'] == 55
  assert render(status).size == (1920, 462)


def test_panel_uses_existing_portrait_upload_geometry():
  from PIL import Image
  frame = Image.new('RGB', (1920, 462))
  frame.putpixel((0, 0), (255, 0, 0))
  frame.putpixel((1919, 461), (0, 255, 0))
  portrait = panel_frame(frame)
  assert portrait.size == (462, 1920)
  assert portrait.getpixel((461, 0)) == (255, 0, 0)
  assert portrait.getpixel((0, 1919)) == (0, 255, 0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Offline Linux service installation')
def test_pinned_diagnostic_install_is_independent_of_model_and_xorg(tmp_path):
  from install_boot_display import configure
  configure(tmp_path, Path(__file__).parent)
  pinned = tmp_path / 'usr/local/lib/carrot-jetlink/boot-display'
  service = (tmp_path / 'etc/systemd/system/carrot-jetlink-boot-display.service').read_text()
  assert 'Requires=' not in service and 'After=local-fs.target systemd-udev-trigger.service' in service
  assert '/opt/carrot-jetlink/current/tools/jetlink/boot_display.py' not in service
  code = '''import sys
sys.path.insert(0, sys.argv[1] + '/tools/jetlink')
import boot_display
import cluster_usb_display
assert not any(name in sys.modules for name in ('pyray', 'tensorrt', 'capnp'))
assert boot_display.render({'uptime':1}).size == (1920,462)
'''
  subprocess.run([sys.executable, '-I', '-c', code, str(pinned)], check=True)


def test_boot_record_does_not_persist_arbitrary_input(tmp_path, monkeypatch):
  original = Path.read_text
  monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k:
                      'test-boot-id' if self.as_posix() == '/proc/sys/kernel/random/boot_id' else original(self, *a, **k))
  boot_status.persist_result(tmp_path, {'stage': 'protected', 'password': 'must-not-save',
                                        'journal': 'private log', 'error': ''})
  value = json.loads((tmp_path / 'BOOT-STATUS.json').read_text())
  assert value == {'format': 1, 'boot_id': 'test-boot-id', 'stage': 'protected', 'error': ''}


@pytest.mark.skipif(sys.platform != 'linux', reason='Real flock/process identity test')
def test_panel_handoff_and_crashed_hud_release(tmp_path):
  from display_owner import hud_requested, panel_owner
  code = '''
import sys,time
from pathlib import Path
from display_owner import panel_owner
with panel_owner(hud=True, directory=Path(sys.argv[1])) as acquired:
 print('HUD_OWNS' if acquired else 'ERROR', flush=True)
 time.sleep(30)
'''
  child = None
  try:
    with panel_owner(directory=tmp_path) as acquired:
      assert acquired
      child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                               env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)),
                               stdout=subprocess.PIPE, text=True)
      deadline = time.monotonic() + 5
      while not hud_requested(tmp_path) and time.monotonic() < deadline:
        time.sleep(.02)
      assert hud_requested(tmp_path)
      assert child.poll() is None
    assert child.stdout.readline().strip() == 'HUD_OWNS'
    with panel_owner(directory=tmp_path) as acquired:
      assert not acquired
    child.kill()
    child.wait(timeout=5)
    with panel_owner(directory=tmp_path) as acquired:
      assert acquired
    (tmp_path / 'hud-request').write_text(json.dumps({'pid': os.getpid(), 'identity': ['old-boot', '0']}))
    assert not hud_requested(tmp_path)
  finally:
    if child and child.poll() is None:
      child.kill()
      child.wait(timeout=5)


def test_runtime_display_override_preserves_immutable_recovery(tmp_path, monkeypatch):
  import install_updates
  target = tmp_path / 'run/systemd/system/carrot-jetlink-boot-display.service.d/runtime.conf'
  monkeypatch.setattr(install_updates, 'BOOT_DISPLAY_DROPIN', target)
  calls = []
  monkeypatch.setattr(install_updates.subprocess, 'run', lambda command, **kwargs: calls.append(command))
  install_updates.configure_runtime_boot_display()
  assert target.read_text() == install_updates.BOOT_DISPLAY_OVERRIDE
  assert not (tmp_path / 'etc').exists()
  assert calls == [['systemctl', 'daemon-reload'], ['systemctl', 'restart', 'carrot-jetlink-boot-display']]
  calls.clear()
  install_updates.migrate_boot_display(tmp_path)
  assert calls == []  # Never restart a healthy panel at every server reconnect.


def test_panel_lock_is_outside_login_managed_shared_memory():
  from display_owner import DIRECTORY
  assert DIRECTORY.parent in (Path('/run'), Path('/tmp'))
