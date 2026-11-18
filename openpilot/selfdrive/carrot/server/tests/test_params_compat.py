import json
from pathlib import Path

import pytest

from openpilot.selfdrive.carrot import carrot_navi
from openpilot.selfdrive.carrot.server.services import params as params_service


class FakeUnknownKeyName(Exception):
  pass


class StaleParams:
  def __init__(self, root: Path) -> None:
    self.root = root

  def get_type(self, key: str):
    raise FakeUnknownKeyName(key)

  def get(self, key: str):
    raise FakeUnknownKeyName(key)

  def get_int(self, key: str):
    raise FakeUnknownKeyName(key)

  def put(self, key: str, value):
    raise FakeUnknownKeyName(key)

  def put_int(self, key: str, value: int):
    raise FakeUnknownKeyName(key)

  def get_param_path(self, key: str = "") -> str:
    return str(self.root / key) if key else str(self.root)


def int_setting(default: int = 0) -> dict:
  return {"min": 0, "max": 60, "default": default}


def test_unregistered_json_setting_uses_atomic_file_fallback(tmp_path, monkeypatch):
  stale_params = StaleParams(tmp_path)
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "Params", lambda: stale_params)
  monkeypatch.setattr(params_service, "UnknownKeyName", FakeUnknownKeyName)

  params_service.set_param_value("FutureIntSetting", 42, int_setting())

  assert (tmp_path / "FutureIntSetting").read_text(encoding="utf-8") == "42"
  assert params_service._read_param_value(stale_params, "FutureIntSetting", 0) == 42


def test_unregistered_fallback_rejects_names_outside_settings(tmp_path, monkeypatch):
  stale_params = StaleParams(tmp_path)
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "Params", lambda: stale_params)
  monkeypatch.setattr(params_service, "UnknownKeyName", FakeUnknownKeyName)

  with pytest.raises(FakeUnknownKeyName):
    params_service.set_param_value("NotInCarrotSettings", 1)


def test_unregistered_read_fallback_rejects_path_traversal(tmp_path):
  stale_params = StaleParams(tmp_path)
  outside = tmp_path.parent / f"{tmp_path.name}_outside"
  outside.write_text("123", encoding="utf-8")

  try:
    assert params_service._read_param_value(stale_params, f"../{outside.name}", 0) == 0
  finally:
    outside.unlink(missing_ok=True)


def test_unregistered_fallback_does_not_hide_other_write_failures(tmp_path, monkeypatch):
  class BrokenParams(StaleParams):
    def put_int(self, key: str, value: int):
      raise OSError("disk full")

  broken_params = BrokenParams(tmp_path)
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "Params", lambda: broken_params)
  monkeypatch.setattr(params_service, "UnknownKeyName", FakeUnknownKeyName)

  with pytest.raises(OSError, match="disk full"):
    params_service.set_param_value("FutureIntSetting", 1, int_setting())


def test_parked_dashcam_toggle_uses_native_params_and_is_excluded_from_restore(monkeypatch):
  from openpilot.common.pv5_parked_dashcam import PARAM

  class NativeParams:
    def __init__(self):
      self.saved = None
      self.writable = True

    def get(self, key):
      assert key == PARAM
      return self.saved

    def put_bool(self, key, enabled):
      assert key == PARAM
      if self.writable:
        self.saved = enabled

  params = NativeParams()
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "Params", lambda: params)
  monkeypatch.setenv("PV5_PARKED_DASHCAM_TRIAL", "1")
  assert params_service.get_param_value(PARAM, 0) is True
  params_service.set_param_value(PARAM, 0)
  assert params_service.get_param_value(PARAM, 0) is False
  params_service.set_param_value(PARAM, 1)
  assert params_service.get_param_value(PARAM, 0) is True
  assert params_service.filter_param_backup_values({PARAM: True}) == {}

  params.writable = False
  with pytest.raises(OSError, match="could not be saved"):
    params_service.set_param_value(PARAM, 0)


def test_parked_dashcam_toggle_rejects_cross_origin_and_invalid_writes(monkeypatch):
  import asyncio

  from aiohttp import web
  from aiohttp.test_utils import TestClient, TestServer

  from openpilot.common.pv5_parked_dashcam import PARAM
  from openpilot.selfdrive.carrot.server.features import params as feature

  writes = []
  monkeypatch.setattr(feature, "set_param_value", lambda *args: writes.append(args[:2]))
  monkeypatch.setattr(feature, "get_param_values", lambda *args: {PARAM: 0})
  monkeypatch.setattr(feature, "get_settings_cached", lambda: ({}, {}, {PARAM: int_setting()}, []))
  monkeypatch.setattr(feature, "append_param_change", lambda *args, **kwargs: None)
  monkeypatch.setattr(feature, "is_drive_engaged", lambda request: False)

  async def run():
    app = web.Application()
    feature.register(app)
    async with TestClient(TestServer(app)) as client:
      url = "/api/param_set"
      on = {"name": PARAM, "value": 1}
      off = {"name": PARAM, "value": 0}
      assert (await client.post(url, json=on, headers={"Origin": "https://other.test"})).status == 403
      assert (await client.post(url, json=on, headers={"Sec-Fetch-Site": "cross-site"})).status == 403
      assert (await client.post(url, data=json.dumps(on))).status == 415
      assert (await client.post(url, json=on | {"value": "1"})).status == 400
      assert writes == []

      origin = str(client.make_url("/").origin())
      for payload in (on, off):
        response = await client.post(url, json=payload, headers={"Origin": origin})
        assert response.status == 200
        assert (await response.json())["value"] == payload["value"]

  asyncio.run(run())
  assert writes == [(PARAM, 1), (PARAM, 0)]


def test_web_params_cannot_write_internal_driver_monitoring_session_state():
  with pytest.raises(ValueError, match="controlled internally"):
    params_service.set_param_value("DriverMonitoringSessionDisabled", True, {"min": 0, "max": 1, "default": 0})


@pytest.mark.parametrize(
  ("enabled", "is_onroad", "clears_alert"),
  ((False, False, True), (False, True, False), (True, False, False)),
)
def test_driver_monitoring_disable_clears_uncertain_alert_only_offroad(monkeypatch, enabled, is_onroad, clears_alert):
  class FakeParams:
    def __init__(self):
      self.removed = []

    def get_bool(self, key: str) -> bool:
      assert key == "IsOnroad"
      return is_onroad

    def put_bool(self, key: str, value: bool) -> None:
      assert (key, value) == ("DriverMonitoringEnabled", enabled)

    def remove(self, key: str) -> None:
      self.removed.append(key)

  fake_params = FakeParams()
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "Params", lambda: fake_params)
  monkeypatch.setattr(params_service, "ParamKeyType", None)

  params_service.set_param_value(
    "DriverMonitoringEnabled",
    enabled,
    {"min": 0, "max": 1, "default": 1},
  )

  assert fake_params.removed == (["Offroad_DriverMonitoringUncertain"] if clears_alert else [])


def test_failed_driver_monitoring_disable_does_not_clear_uncertain_alert(monkeypatch):
  class BrokenParams:
    removed = []

    def put_bool(self, key: str, value: bool) -> None:
      raise OSError("disk full")

    def remove(self, key: str) -> None:
      self.removed.append(key)

  broken_params = BrokenParams()
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "Params", lambda: broken_params)
  monkeypatch.setattr(params_service, "ParamKeyType", None)

  with pytest.raises(OSError, match="disk full"):
    params_service.set_param_value(
      "DriverMonitoringEnabled",
      False,
      {"min": 0, "max": 1, "default": 1},
    )

  assert broken_params.removed == []


def test_restore_rejects_internal_driver_monitoring_session_state(monkeypatch):
  # The internal-key branch does not need a native Params registry, but the
  # preview API performs its environment check before iterating entries.
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "ParamKeyType", object())
  monkeypatch.setattr(params_service, "Params", object)
  monkeypatch.setattr(params_service, "get_param_values", lambda names, defaults: {})

  preview = params_service.preview_param_restore_values({"DriverMonitoringSessionDisabled": True})
  assert preview["summary"] == {"changed": 0, "same": 0, "skipped": 0, "invalid": 1, "selected": 0}
  assert preview["entries"][0]["reason"] == "controlled internally"
  assert not preview["entries"][0]["apply"]

  restored = params_service.restore_param_values_validated({"DriverMonitoringSessionDisabled": True})
  assert restored["result"] == {"ok_cnt": 0, "fail_cnt": 0, "fails": []}


def test_driver_monitoring_enabled_is_excluded_from_file_and_qr_backups(monkeypatch):
  class FakeParamKeyType:
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    TIME = "time"
    STRING = "string"
    JSON = "json"
    BYTES = "bytes"

  class BackupParams:
    def all_keys(self):
      return ["DriverMonitoringEnabled", "DriverMonitoringMode", "CarrotVisionEnabled"]

    def get_type(self, key):
      return FakeParamKeyType.INT if key == "DriverMonitoringMode" else FakeParamKeyType.BOOL

    def get_default_value(self, key):
      return "0" if key == "DriverMonitoringMode" else "1"

    def get(self, key, **_kwargs):
      return {
        "DriverMonitoringEnabled": "0",
        "DriverMonitoringMode": "1",
        "CarrotVisionEnabled": "1",
      }[key]

  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "ParamKeyType", FakeParamKeyType)
  monkeypatch.setattr(params_service, "Params", BackupParams)

  assert params_service.get_all_param_values_for_backup() == {
    "DriverMonitoringMode": "1",
    "CarrotVisionEnabled": "1",
  }
  # Keep the existing QR schema so older QR codes still decode the key and the
  # restore filter can safely ignore its value.
  assert params_service._backup_param_names() == [
    "DriverMonitoringEnabled",
    "DriverMonitoringMode",
    "CarrotVisionEnabled",
  ]

  qr = params_service.build_params_qr_payload({
    "DriverMonitoringEnabled": "0",
    "DriverMonitoringSessionDisabled": "1",
    "DriverMonitoringMode": "1",
    "CarrotVisionEnabled": "1",
  })
  assert qr["count"] == 2
  assert params_service.parse_params_qr_payload(qr["payload"]) == {
    "DriverMonitoringMode": "1",
    "CarrotVisionEnabled": "1",
  }

  # Simulate a QR made before the exclusion policy. The stable schema still
  # decodes the key, but restore refuses to apply it even when selected.
  old_qr = params_service._build_params_qr_payload_v4({
    "DriverMonitoringEnabled": "0",
    "DriverMonitoringMode": "1",
  })
  old_values = params_service.parse_params_qr_payload(old_qr["payload"])
  assert old_values == {
    "DriverMonitoringEnabled": "0",
    "DriverMonitoringMode": "1",
  }
  monkeypatch.setattr(params_service, "get_param_values", lambda names, defaults: {
    "DriverMonitoringEnabled": True,
    "DriverMonitoringMode": 0,
  })
  restored = params_service.restore_param_values_validated(
    old_values,
    selected_keys=["DriverMonitoringEnabled"],
  )
  entries = {entry["key"]: entry for entry in restored["preview"]["entries"]}
  assert entries["DriverMonitoringEnabled"]["reason"] == "excluded from backup restore"
  assert not entries["DriverMonitoringEnabled"]["apply"]
  assert restored["result"] == {"ok_cnt": 0, "fail_cnt": 0, "fails": []}


def test_driver_monitoring_enabled_from_old_backup_is_not_restored(monkeypatch):
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "ParamKeyType", object())
  monkeypatch.setattr(params_service, "Params", object)
  monkeypatch.setattr(params_service, "get_param_values", lambda names, defaults: {})

  preview = params_service.preview_param_restore_values(
    {"DriverMonitoringEnabled": "0"},
    selected_keys=["DriverMonitoringEnabled"],
  )
  assert preview["summary"] == {"changed": 0, "same": 0, "skipped": 1, "invalid": 0, "selected": 0}
  assert preview["entries"][0]["reason"] == "excluded from backup restore"
  assert not preview["entries"][0]["apply"]

  restored = params_service.restore_param_values_from_backup({"DriverMonitoringEnabled": "0"})
  assert restored == {"ok_cnt": 0, "fail_cnt": 0, "fails": []}


def test_reset_defaults_restores_driver_monitoring_enabled_to_on(monkeypatch):
  class FakeParamKeyType:
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    TIME = "time"
    STRING = "string"
    JSON = "json"
    BYTES = "bytes"

  class RestoreParams:
    def get_type(self, key):
      assert key == "DriverMonitoringEnabled"
      return FakeParamKeyType.BOOL

    def get_bool(self, key):
      assert key == "DriverMonitoringEnabled"
      return False

  writes = []
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "ParamKeyType", FakeParamKeyType)
  monkeypatch.setattr(params_service, "Params", RestoreParams)
  monkeypatch.setattr(params_service, "_catalog_definitions", dict)
  monkeypatch.setattr(params_service, "get_param_values", lambda names, defaults: {"DriverMonitoringEnabled": False})
  monkeypatch.setattr(params_service, "set_param_value", lambda key, value, definition=None: writes.append((key, value)))

  restored = params_service.restore_param_values_validated(
    {"DriverMonitoringEnabled": "1"},
    source="reset_defaults",
  )

  assert restored["preview"]["summary"] == {
    "changed": 1,
    "same": 0,
    "skipped": 0,
    "invalid": 0,
    "selected": 1,
  }
  assert restored["preview"]["entries"][0]["value"] is True
  assert restored["result"] == {"ok_cnt": 1, "fail_cnt": 0, "fails": []}
  assert writes == [("DriverMonitoringEnabled", True)]


def test_old_params_backup_file_is_filtered_before_download(tmp_path):
  backup_path = tmp_path / "params_backup.json"
  backup_path.write_text(json.dumps({
    "DriverMonitoringEnabled": "0",
    "DriverMonitoringSessionDisabled": "1",
    "DriverMonitoringMode": "1",
  }), encoding="utf-8")
  assert params_service.read_param_backup_values(str(backup_path)) == {"DriverMonitoringMode": "1"}


def test_map_param_reader_reads_map_fps_file_while_native_registry_is_stale(tmp_path):
  stale_params = StaleParams(tmp_path)
  (tmp_path / "ClusterNaviMapFps").write_text("3", encoding="utf-8")

  reader = carrot_navi.ClusterNaviMapParamReader(stale_params)

  assert reader._read_int("ClusterNaviMapFps", 1) == 3
