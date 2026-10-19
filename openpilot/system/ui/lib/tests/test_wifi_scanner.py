from pytest_mock import MockerFixture

from openpilot.system.ui.lib.wifi_manager import ConnectStatus, WifiManager, WifiState


def _run_scanner_once(mocker: MockerFixture, *, active: bool, status: ConnectStatus, elapsed: float):
  wm = WifiManager.__new__(WifiManager)
  wm._exit = False
  wm._active = active
  wm._wifi_state = WifiState(status=status)
  wm._last_network_scan = 100.0
  wm._request_scan = mocker.MagicMock()

  mocker.patch("openpilot.system.ui.lib.wifi_manager.time.monotonic", side_effect=[100.0 + elapsed, 100.0 + elapsed])

  def stop_after_iteration(_):
    wm._exit = True

  mocker.patch("openpilot.system.ui.lib.wifi_manager.time.sleep", side_effect=stop_after_iteration)
  wm._network_scanner()
  return wm


def test_background_scans_after_five_seconds_when_disconnected(mocker):
  wm = _run_scanner_once(mocker, active=False, status=ConnectStatus.DISCONNECTED, elapsed=5.1)

  wm._request_scan.assert_called_once_with()


def test_background_does_not_scan_while_connected(mocker):
  wm = _run_scanner_once(mocker, active=False, status=ConnectStatus.CONNECTED, elapsed=60.0)

  wm._request_scan.assert_not_called()


def test_direct_ap_suppresses_only_disconnected_background_scan(mocker):
  mocker.patch("openpilot.system.ui.lib.wifi_manager.direct_ap_active", return_value=True)
  wm = _run_scanner_once(mocker, active=False, status=ConnectStatus.DISCONNECTED, elapsed=60.0)
  wm._request_scan.assert_not_called()


def test_user_network_page_can_scan_with_direct_ap(mocker):
  mocker.patch("openpilot.system.ui.lib.wifi_manager.direct_ap_active", return_value=True)
  wm = _run_scanner_once(mocker, active=True, status=ConnectStatus.DISCONNECTED, elapsed=60.0)
  wm._request_scan.assert_called_once_with()


def test_network_ui_prefers_station_over_new_ap(mocker):
  from types import SimpleNamespace
  import openpilot.system.ui.lib.wifi_manager as module
  wm = WifiManager.__new__(WifiManager)
  wm._exit = True
  wm._nm = 'nm'
  mocker.patch.object(module, 'DBusAddress', side_effect=lambda path, **kw: path)
  mocker.patch.object(module, 'new_method_call', return_value='devices')
  mocker.patch.object(module, 'Properties', side_effect=lambda addr: SimpleNamespace(get=lambda key: (addr, key)))
  def reply(message):
    if message == 'devices':
      return SimpleNamespace(body=[['/ap', '/station']])
    path, prop = message
    value = module.NM_DEVICE_TYPE_WIFI if prop == 'DeviceType' else {'/ap': 'wljetson0', '/station': 'wlan0'}[path]
    return SimpleNamespace(body=[('u' if prop == 'DeviceType' else 's', value)])
  wm._router_main = SimpleNamespace(send_and_get_reply=reply)
  assert wm._get_adapter(module.NM_DEVICE_TYPE_WIFI) == '/station'
