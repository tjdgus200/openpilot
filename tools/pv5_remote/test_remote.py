"""Isolated PC checks. Never connect to the vehicle or a production endpoint."""

import base64
from pathlib import Path
import tempfile
import unittest

from openpilot.system.pv5_incidents.archive_protocol import encode, headers
from tools.pv5_remote.conditional_remote import HOME_POLICY, Gate, eligible, home_policy, public_key, ssh_config, unit_text
from tools.pv5_remote.pc import Bootstrap, PATH, QUALIFICATION


def key(byte):
  raw = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes([byte]) * 32
  return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


def home(now=10.0):
  return {
    'deviceState': {'stamp': now, 'valid': True, 'started': False, 'cpu': [10, 10, 10, 10], 'thermal': 0, 'memory': 40, 'free': 60},
    'pandaStates': {'stamp': now, 'valid': True, 'disconnected': True},
    'managerState': {
      'stamp': now,
      'valid': True,
      'processes': dict(
        [(name, [True, True]) for name in ('hardwared', 'pandad')]
        + [(name, [False, False]) for name in ('card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd')]
      ),
    },
  }


def parked(now=10.0):
  value = home(now)
  value['deviceState']['started'] = True
  value.update(
    {
      'carParams': {'stamp': now, 'valid': True, 'vehicle': 'KIA_PV5', 'notCar': False, 'dashcam': True},
      'carState': {
        'stamp': now,
        'valid': True,
        'gear': 'park',
        'raw_speed': 0.0,
        'speed': 0.0,
        'can_valid': True,
        'can_timeout': False,
        'standstill': True,
        'cruise': False,
        'aeb': False,
      },
      'carControl': {'stamp': now, 'valid': True, 'enabled': False, 'lat': False, 'long': False},
      'selfdriveState': {'stamp': now, 'valid': True, 'enabled': False, 'active': False, 'state': 'disabled', 'critical': False},
      'onroadEvents': {'stamp': now, 'valid': True, 'disable': False},
    }
  )
  return value


class GateChecks(unittest.TestCase):
  def test_home_and_vehicle_modes_are_separately_qualified(self):
    self.assertEqual(eligible(home(), 10, ['detached_home']), ('detached_home', ''))
    self.assertEqual(eligible(parked(), 10, ['parked_dashcam']), ('parked_dashcam', ''))
    self.assertFalse(eligible(parked(), 10, ['detached_home'])[0])
    self.assertFalse(eligible(home(), 10, ['parked_dashcam'])[0])

  def test_normal_mode_zero_speed_is_never_full_remote(self):
    value = parked()
    value['carParams']['dashcam'] = False
    self.assertEqual(eligible(value, 10, ['detached_home', 'parked_dashcam']), ('', 'normal_mode_code_reports_only'))

  def test_each_vehicle_control_movement_emergency_and_missing_value_refuses(self):
    for service, field, value in [
      ('carState', 'cruise', True),
      ('carState', 'aeb', True),
      ('carState', 'gear', 'drive'),
      ('carState', 'raw_speed', 0.001),
      ('carState', 'speed', 0.011),
      ('carState', 'speed', float('nan')),
      ('carState', 'can_valid', False),
      ('carState', 'can_timeout', True),
      ('carState', 'standstill', False),
      ('carControl', 'enabled', True),
      ('carControl', 'lat', True),
      ('carControl', 'long', True),
      ('selfdriveState', 'enabled', True),
      ('selfdriveState', 'active', True),
      ('selfdriveState', 'critical', True),
      ('onroadEvents', 'disable', True),
      ('carParams', 'vehicle', 'OTHER'),
      ('carParams', 'notCar', True),
    ]:
      state = parked()
      state[service][field] = value
      self.assertFalse(eligible(state, 10, ['parked_dashcam'])[0], (service, field))

  def test_stale_invalid_future_and_resource_conditions(self):
    for service in ('deviceState', 'pandaStates', 'managerState', 'carState', 'carControl', 'selfdriveState', 'onroadEvents'):
      for field, value in [('stamp', 0.0), ('stamp', 10.000001), ('valid', False)]:
        state = parked()
        state[service][field] = value
        self.assertFalse(eligible(state, 10, ['parked_dashcam'])[0], (service, field))
    for field, value in [('cpu', []), ('cpu', [71]), ('cpu', [float('nan')]), ('thermal', 1), ('memory', 81), ('free', 19)]:
      state = parked()
      state['deviceState'][field] = value
      self.assertFalse(eligible(state, 10, ['parked_dashcam'])[0])

  def test_uncapped_home_load_and_vehicle_reconnection_keep_distinct_admission(self):
    state = home()
    state['deviceState'].update(cpu=[100] * 8, memory=99, free=1)
    self.assertEqual(eligible(state, 10, ['detached_home']), ('detached_home', ''))
    state['pandaStates']['disconnected'] = False
    self.assertEqual(eligible(state, 10, ['detached_home']), ('', 'not_detached'))
    state = home()
    state['deviceState']['thermal'] = 1
    self.assertEqual(eligible(state, 10, ['detached_home']), ('', 'resource'))

  def test_uncapped_resource_policy_cannot_be_used_for_vehicle_or_legacy_config(self):
    home_policy({'qualified_modes': ['detached_home'], 'resource_policy': HOME_POLICY})
    for modes, policy in [(['parked_dashcam'], HOME_POLICY), (['detached_home', 'parked_dashcam'], HOME_POLICY),
                          (['detached_home'], None), ([], HOME_POLICY)]:
      with self.assertRaisesRegex(ValueError, 'home_resource_policy_required'):
        home_policy({'qualified_modes': modes, 'resource_policy': policy})

  def test_home_requires_real_disconnection_and_stopped_producers(self):
    state = home()
    state['pandaStates']['disconnected'] = False
    self.assertFalse(eligible(state, 10, ['detached_home'])[0])
    for name in ('card', 'controlsd', 'selfdrived', 'modeld', 'loggerd', 'encoderd', 'hardwared', 'pandad'):
      state = home()
      state['managerState']['processes'][name][0] = not state['managerState']['processes'][name][0]
      self.assertFalse(eligible(state, 10, ['detached_home'])[0])

  def test_auto_reentry_without_expiry_and_fail_closed_active_delay(self):
    gate = Gate()
    for stamp in (0, 0.5, 1, 1.5):
      self.assertEqual(gate.tick('detached_home', stamp), 'wait')
    self.assertEqual(gate.tick('detached_home', 2), 'open')
    for index in range(1, 10001):
      self.assertEqual(gate.tick('detached_home', 2 + index / 10), 'keep')
    self.assertEqual(gate.tick('', 1002.1), 'close')
    reopened = Gate()  # PID1 starts a fresh observer; no user reauthentication.
    for stamp in (1008, 1008.5, 1009, 1009.5):
      self.assertEqual(reopened.tick('detached_home', stamp), 'wait')
    self.assertEqual(reopened.tick('detached_home', 1010), 'open')
    self.assertEqual(reopened.tick('detached_home', 1010.26), 'close')

  def test_unit_and_auth_boundaries(self):
    text = unit_text('/usr/local/venv/bin/python3')
    for setting in (
      'WatchdogSec=1s',
      'NotifyAccess=main',
      'KillMode=control-group',
      'Restart=always',
      'ProtectControlGroups=yes',
      'NoNewPrivileges=yes',
      'InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private',
    ):
      self.assertIn(setting, text)
    self.assertNotIn('RuntimeMaxSec', text)
    for setting in ('CPUQuota=', 'MemoryMax=', 'CPUAffinity=', 'CPUSchedulingPolicy=idle'):
      self.assertNotIn(setting, text)
    config = ssh_config()
    for setting in (
      'UsePAM yes',
      'ListenAddress 127.0.0.1',
      'AuthenticationMethods publickey',
      'PasswordAuthentication no',
      'PermitRootLogin prohibit-password',
      'AllowTcpForwarding no',
      'PermitUserRC no',
    ):
      self.assertIn(setting, config)
    self.assertEqual(public_key(key(3)), key(3))


class BootstrapChecks(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.root, self.secret = Path(self.temp.name), bytes(32)
    (self.root / 'client_ed25519.pub').write_text(key(1))
    self.bootstrap = Bootstrap(self.root, self.secret, lambda: True)

  def tearDown(self):
    self.temp.cleanup()

  def test_auth_first_pin_repeat_identical_host_only_and_no_private_key_output(self):
    body = encode({'version': 1, 'host_key': key(2)})
    result = self.bootstrap.ingest(PATH, headers(self.secret, PATH, body, now=1000), body, 1000)
    self.assertTrue(result['host_key_pinned'])
    self.assertEqual(result['client_public_key'], key(1))
    self.assertNotIn('private', encode(result).decode())
    self.bootstrap.ingest(PATH, headers(self.secret, PATH, body, now=1000), body, 1000)
    changed = encode({'version': 1, 'host_key': key(3)})
    with self.assertRaisesRegex(ValueError, 'host_key_changed'):
      self.bootstrap.ingest(PATH, headers(self.secret, PATH, changed, now=1000), changed, 1000)
    self.assertEqual((self.root / 'host_key.pub').read_text().strip(), key(2))

  def test_bad_auth_replay_disabled_and_tampering_never_write_host(self):
    body = encode({'version': 1, 'host_key': key(2)})
    signed = headers(self.secret, PATH, body, now=1000)
    for path, raw, stamp in [(PATH, body, 1400), (PATH + '/bad', body, 1000), (PATH, body + b' ', 1000)]:
      with self.assertRaises(PermissionError):
        self.bootstrap.ingest(path, signed, raw, stamp)
    self.assertFalse((self.root / 'host_key.pub').exists())
    self.bootstrap.enabled = lambda: False
    with self.assertRaises(PermissionError):
      self.bootstrap.ingest(PATH, signed, body, 1000)
    self.bootstrap.enabled = lambda: True
    self.bootstrap.ingest(PATH, signed, body, 1000)
    with self.assertRaises(PermissionError):
      self.bootstrap.ingest(PATH, signed, body, 1000)

  def test_qualification_needs_same_host_fresh_real_check_and_ignores_untrusted_commands(self):
    body = encode({'version': 1, 'host_key': key(2)})

    def request(path=QUALIFICATION, stamp=1000, data=body):
      return self.bootstrap.ingest(path, headers(self.secret, path, data, now=stamp), data, stamp)

    with self.assertRaises(ValueError):
      request()
    request(PATH)
    with self.assertRaises(ValueError):
      request()
    for value in ({'version': True, 'host_key': key(2)}, {'version': 1, 'host_key': key(2), 'command': 'reboot'}):
      with self.assertRaises(ValueError):
        request(PATH, data=encode(value))
    proof = {'passed': True, 'host_key': key(2), 'stamp': 999, 'guard_sha256': 'a' * 64}
    (self.root / 'home-qualification.json').write_bytes(encode(proof))
    with self.assertRaises(ValueError):
      request()
    proof['resource_policy'] = HOME_POLICY
    (self.root / 'home-qualification.json').write_bytes(encode(proof))
    qualified = request()
    self.assertTrue(qualified['home_qualified'])
    self.assertEqual(qualified['resource_policy'], HOME_POLICY)
    with self.assertRaises(ValueError):
      request(stamp=3000)


if __name__ == '__main__':
  unittest.main()
