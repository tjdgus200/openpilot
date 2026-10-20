"""Fake sysfs only: no hardware, USB power or real voter is changed."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld.jetlink import usb_power


@pytest.fixture
def power_tree(tmp_path):
  marker = tmp_path / 'AGNOS'
  marker.touch()
  pd = tmp_path / 'usbpd'
  pd.mkdir()
  voter = tmp_path / 'USB_ICL'
  voter.mkdir()
  for path, value in [(pd / 'current_pr', 'none'), (voter / 'force_active', '0'), (voter / 'force_val', '-1')]:
    path.write_text(value + '\n')
  env = {**os.environ, 'JETLINK_AGNOS_MARKER': str(marker), 'JETLINK_USBPD': str(pd),
         'JETLINK_USB_ICL_VOTER': str(voter), 'JETLINK_USB_POWER_STATE': str(tmp_path / 'state'),
         'JETLINK_USB_POWER_POLICY_DIR': str(tmp_path / 'policy')}
  def run(action):
    return subprocess.run(['bash', str(Path(__file__).with_name('usb_power.sh')), action],
                          env=env, capture_output=True, text=True, check=False)
  return SimpleNamespace(pd=pd, voter=voter, env=env, run=run)


def test_empty_port_apply_persists_and_restore_requires_empty_port(power_tree):
  t = power_tree
  assert t.run('off').returncode == 0
  assert (t.voter / 'force_val').read_text().strip() == '0'
  assert (t.voter / 'force_active').read_text().strip() == '1'
  t.pd.joinpath('current_pr').write_text('sink\n')
  assert t.run('off').returncode == 0  # zero already set: no change on replug
  assert t.run('on').returncode == 3  # never re-enable current into an attached host
  assert t.voter.joinpath('force_active').read_text().strip() == '1'
  t.pd.joinpath('current_pr').write_text('none\n')
  assert t.run('on').returncode == 0
  assert t.voter.joinpath('force_active').read_text().strip() == '0'
  assert t.voter.joinpath('force_val').read_text().strip() == '-1'


@pytest.mark.parametrize('role', ['sink', 'source', '', 'unknown'])
def test_attached_or_unknown_port_never_cuts_usb_only_supply(power_tree, role):
  t = power_tree
  t.pd.joinpath('current_pr').write_text(role + '\n')
  assert t.run('off').returncode == 3
  assert t.voter.joinpath('force_active').read_text().strip() == '0'
  assert t.voter.joinpath('force_val').read_text().strip() == '-1'


def test_foreign_forced_limit_is_not_overwritten_or_released(power_tree):
  t = power_tree
  t.voter.joinpath('force_active').write_text('1\n')
  t.voter.joinpath('force_val').write_text('500000\n')
  assert t.run('off').returncode == 1
  assert t.run('on').stdout.strip() == 'not-owned'
  assert t.voter.joinpath('force_val').read_text().strip() == '500000'


def test_foreign_zero_vote_is_not_claimed_or_released(power_tree):
  t = power_tree
  t.voter.joinpath('force_active').write_text('1\n')
  t.voter.joinpath('force_val').write_text('0\n')
  assert t.run('off').stdout.strip() == 'disabled'
  assert t.run('on').stdout.strip() == 'not-owned'
  assert t.voter.joinpath('force_active').read_text().strip() == '1'


def test_missing_attributes_fail_without_writes(power_tree):
  t = power_tree
  t.pd.joinpath('current_pr').unlink()
  assert t.run('off').returncode == 1
  assert t.voter.joinpath('force_active').read_text().strip() == '0'


def test_guard_defers_until_empty_then_runs_once_outside_frame_loop(tmp_path, monkeypatch):
  role = tmp_path / 'current_pr'
  role.write_text('sink\n')
  monkeypatch.setattr(usb_power, 'POWER_ROLE', role)
  calls = []
  monkeypatch.setattr(usb_power.os, 'geteuid', lambda: 1000)
  monkeypatch.setattr(usb_power.subprocess, 'run', lambda *a, **kw: calls.append((a, kw)) or SimpleNamespace(returncode=0, stdout='disabled\n', stderr=''))
  guard = usb_power.UsbPowerGuard()
  guard.enabled = True
  guard.step()
  assert calls == []
  role.write_text('none\n')
  guard.step()
  role.write_text('sink\n')
  guard.step()
  assert guard.applied and len(calls) == 1
  assert calls[0][0][0][:3] == ['sudo', '-n', 'bash']
  assert calls[0][1]['timeout'] == 2


@pytest.mark.parametrize('result', [SimpleNamespace(returncode=1, stdout='', stderr='no voter'), SimpleNamespace(returncode=0, stdout='unexpected', stderr='')])
def test_failed_guard_does_not_report_success_or_repeatedly_spawn(tmp_path, monkeypatch, result):
  role = tmp_path / 'current_pr'
  role.write_text('none\n')
  monkeypatch.setattr(usb_power, 'POWER_ROLE', role)
  calls = []
  monkeypatch.setattr(usb_power.subprocess, 'run', lambda *a, **kw: calls.append(a) or result)
  guard = usb_power.UsbPowerGuard()
  guard.enabled = True
  guard.step()
  guard.step()
  assert guard.failed and not guard.applied and len(calls) == 1


def test_daemon_applies_before_attachment_check_and_never_restores_on_exit():
  text = (Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/modeld/jetlink/daemon.py').read_text()
  main = text.split('def main():', 1)[1]
  assert main.index('usb_power.step()') < main.index('if SESSION_DETACHED or not host_attached():')
  assert main.count('usb_power.step()') == 1
  assert 'usb_power.restore' not in main


def test_explicit_independent_supply_applies_attached_and_survives_new_boot_state(power_tree):
  t = power_tree
  t.pd.joinpath('current_pr').write_text('sink\n')
  assert t.run('independent').stdout.strip() == 'disabled'
  policy = Path(t.env['JETLINK_USB_POWER_POLICY_DIR']) / 'independent-power'
  assert policy.read_text() == 'confirmed\n'
  assert 'policy=independent' in t.run('status').stdout
  assert t.run('on').returncode == 3
  assert policy.exists()
  # A reboot clears the forced vote, while owner enrollment stays on /data.
  t.voter.joinpath('force_active').write_text('0\n')
  assert t.run('off').stdout.strip() == 'disabled'
  assert t.voter.joinpath('force_active').read_text().strip() == '1'
  t.pd.joinpath('current_pr').write_text('none\n')
  assert t.run('on').stdout.strip() == 'restored'
  assert not policy.exists()


@pytest.mark.parametrize('kind', ['symlink', 'writable', 'malformed'])
def test_untrusted_confirmation_never_authorizes_attached_writes(power_tree, kind):
  t = power_tree
  directory = Path(t.env['JETLINK_USB_POWER_POLICY_DIR'])
  directory.mkdir(mode=0o755)
  flag = directory / 'independent-power'
  if kind == 'symlink':
    target = directory / 'other'
    target.write_text('confirmed\n')
    flag.symlink_to(target)
  else:
    flag.write_text('confirmed\n' if kind == 'writable' else 'invalid\n')
    flag.chmod(0o666 if kind == 'writable' else 0o644)
  t.pd.joinpath('current_pr').write_text('sink\n')
  assert t.run('off').returncode != 0
  assert t.voter.joinpath('force_active').read_text().strip() == '0'


def test_independent_policy_does_not_overwrite_foreign_current_limit(power_tree):
  t = power_tree
  t.voter.joinpath('force_active').write_text('1\n')
  t.voter.joinpath('force_val').write_text('500000\n')
  assert t.run('independent').returncode == 1
  assert not Path(t.env['JETLINK_USB_POWER_POLICY_DIR']).exists()


def test_guard_uses_explicit_independent_supply_without_detaching(tmp_path, monkeypatch):
  role = tmp_path / 'current_pr'
  role.write_text('sink\n')
  monkeypatch.setattr(usb_power, 'POWER_ROLE', role)
  monkeypatch.setattr(usb_power, 'independent_power_confirmed', lambda: True)
  calls = []
  monkeypatch.setattr(usb_power.subprocess, 'run', lambda *a, **kw: calls.append(a) or SimpleNamespace(returncode=0, stdout='disabled\n', stderr=''))
  guard = usb_power.UsbPowerGuard()
  guard.enabled = True
  guard.step()
  guard.step()
  assert guard.applied and len(calls) == 1
