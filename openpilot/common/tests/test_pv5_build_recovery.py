"""PC-only startup fault injection; never runs SCons or touches device /data."""
import ast
import io
import os
from pathlib import Path
import shlex
import stat
import subprocess
from types import SimpleNamespace

import pytest

from openpilot.common.scons_cache_lock import acquire_cache_lock
from openpilot.system.manager import pv5_build_recovery as scope


ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("brand,fingerprint,not_car,expected", (
  ("hyundai", "KIA_PV5", False, True), ("hyundai", "KIA_EV6", False, False),
  ("toyota", "KIA_PV5", False, False), ("", "", False, False),
  ("hyundai", "KIA_PV5", True, False),
))
def test_persisted_car_identity_is_read_without_native_params(tmp_path, monkeypatch, brand, fingerprint, not_car, expected):
  from openpilot import cereal
  # The prepared PC runner places the real schema at an ASCII path for pycapnp.
  monkeypatch.setattr(scope, "__file__", str(cereal.REPO_ROOT / "openpilot/system/manager/pv5_build_recovery.py"))
  cp = cereal.car.CarParams.new_message(brand=brand, carFingerprint=fingerprint, notCar=not_car)
  (tmp_path / "CarParamsPersistent").write_bytes(cp.to_bytes())
  assert scope.pv5_build_recovery_enabled(tmp_path) is expected


@pytest.mark.parametrize("raw", (None, b"", b"broken", bytes(scope.MAX_CAR_PARAMS_BYTES + 1)))
def test_unknown_or_corrupt_car_does_not_admit_repair(tmp_path, raw):
  if raw is not None:
    (tmp_path / "CarParamsPersistent").write_bytes(raw)
  assert not scope.pv5_build_recovery_enabled(tmp_path)


def functions():
  tree = ast.parse((ROOT / "openpilot/system/manager/build.py").read_text())
  nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
           ("build", "remove_empty_build_outputs", "is_cache_lock_failure")]
  namespace = {"os": os, "stat": stat, "Path": Path, "acquire_cache_lock": acquire_cache_lock,
                   "Spinner": object, "AGNOS": False}
  exec(compile(ast.Module(body=nodes, type_ignores=[]), "build.py", "exec"), namespace)
  return namespace


def test_empty_output_cleanup_keeps_source_nonempty_files_and_symlinks(tmp_path):
  root, cache = tmp_path / "repo", tmp_path / "cache"
  root.mkdir()
  cache.mkdir()
  for name in ("empty.o", "empty.os", "empty.a", "source.py", "shared.so"):
    (root / name).touch()
  (root / "real.o").write_bytes(b"object")
  outside = tmp_path / "external.o"
  outside.touch()
  (root / "linked.o").symlink_to(outside)
  (root / "outside").symlink_to(tmp_path, target_is_directory=True)
  (root / ".git").mkdir()
  (root / ".git/private.o").touch()
  assert functions()["remove_empty_build_outputs"](root, cache) == 3
  assert (root / "real.o").read_bytes() == b"object"
  assert all((root / p).exists() for p in ("source.py", "shared.so", "linked.o", ".git/private.o"))
  assert outside.exists()


def test_busy_cache_preserves_all_outputs(tmp_path):
  root, cache = tmp_path / "repo", tmp_path / "cache"
  root.mkdir()
  cache.mkdir()
  (root / "empty.o").touch()
  with acquire_cache_lock(cache):
    assert functions()["remove_empty_build_outputs"](root, cache) == 0
  assert (root / "empty.o").exists()


@pytest.mark.parametrize("pv5,empty,agnos,expected", (
  (True, True, True, ("--cache-populate", "--cache-disable")),
  (False, True, True, ("--cache-populate",) * 3),
  (True, False, True, ("--cache-populate",) * 3),
  (True, True, False, ("--cache-populate",) * 3),
))
def test_failed_build_gets_only_one_scoped_recovery_attempt(tmp_path, pv5, empty, agnos, expected):
  namespace = functions()
  root, cache = tmp_path / "repo", tmp_path / "cache"
  root.mkdir()
  cache.mkdir()
  if empty:
    (root / "empty.o").touch()
  commands = []
  def popen(command, **kwargs):
    commands.append(command)
    return SimpleNamespace(returncode=1, poll=lambda: 1, stdout=io.BytesIO(b"compile failed"))
  fake_os = SimpleNamespace(environ={"CI": "1"}, cpu_count=lambda: 8,
                            sched_setaffinity=lambda *a: None, getenv=lambda k: "1", walk=os.walk)
  namespace.update(os=fake_os, subprocess=SimpleNamespace(Popen=popen, PIPE=-1, STDOUT=-2),
                   AGNOS=agnos, BASEDIR=str(root), CACHE_DIR=cache,
                   HARDWARE=SimpleNamespace(set_power_save=lambda *a: None),
                   pv5_build_recovery_enabled=lambda: pv5, add_file_handler=lambda *a: None,
                   cloudlog=SimpleNamespace(error=lambda *a: None))
  with pytest.raises(SystemExit):
    namespace["build"](SimpleNamespace(update=lambda *a: None, close=lambda: None))
  assert tuple(command[2] for command in commands) == expected
  assert commands[-1][1] == "-j1"
  assert (root / "empty.o").exists() is (empty and not (pv5 and agnos))


@pytest.mark.parametrize("pv5,interrupted,explicit", (
  (True, True, False), (True, False, False), (False, True, False),
  (False, False, True), (True, True, True),
))
def test_launcher_marker_and_existing_explicit_rebuild_requests(tmp_path, pv5, interrupted, explicit):
  source = (ROOT / "launch_chffrplus.sh").read_text()
  function = source[source.index("function consume_rebuild_request {"):source.index("function start_manager {")]
  request, marker, log = (tmp_path / name for name in ("request", "marker", "log"))
  if interrupted:
    marker.touch()
  if explicit:
    request.touch()
  script = (f"PV5_BUILD_RECOVERY={int(pv5)}\nREBUILD_REQUEST={shlex.quote(str(request))}\n" +
            f"PV5_BUILD_MARKER={shlex.quote(str(marker))}\nREBUILD_LOG={shlex.quote(str(log))}\n" +
            'DIR=/unused\nreset_build_state() { echo repaired; }\nscons() { return 0; }\n' +
            function + "\nconsume_rebuild_request\n")
  result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
  assert result.returncode == 0, result.stderr
  assert marker.exists() is (pv5 or interrupted)
  assert log.exists() is (explicit or (pv5 and interrupted))
  assert not request.exists()
  check_at = source.index('run_startup_command python3 "$DIR/openpilot/system/manager/params_check.py"',
                          source.index('run_startup_command ./build.py'))
  assert source.index('rm -f "$PV5_BUILD_MARKER"') > check_at


def test_marker_is_retained_when_cleanup_fails(tmp_path):
  source = (ROOT / "launch_chffrplus.sh").read_text()
  function = source[source.index("function consume_rebuild_request {"):source.index("function start_manager {")]
  marker, request = tmp_path / "marker", tmp_path / "request"
  marker.touch()
  script = (f"PV5_BUILD_RECOVERY=1\nPV5_BUILD_MARKER={shlex.quote(str(marker))}\n" +
            f"REBUILD_REQUEST={shlex.quote(str(request))}\nREBUILD_LOG={shlex.quote(str(tmp_path / 'log'))}\n" +
            'DIR=/unused\nreset_build_state() { return 1; }\nscons() { return 0; }\n' +
            function + "\nconsume_rebuild_request\n")
  result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
  assert result.returncode != 0
  assert marker.exists() and request.exists()
