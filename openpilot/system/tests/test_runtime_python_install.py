import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openpilot.common.basedir import BASEDIR


def _bash() -> str:
  # Windows' bash.exe alias may launch WSL instead of a usable local shell.
  git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
  executable = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
  if executable is None:
    pytest.skip("bash is required for launcher integration tests")
  return executable


def _install(tmp_path: Path, *, installer="pip", offline_fail=False, online_fail=False,
             installed=False, broken_import=False, required=True, device=False, dependencies=False):
  source = (Path(BASEDIR) / "launch_chffrplus.sh").read_text(encoding="utf-8")
  functions = source[source.index("function install_runtime_python_package {"):source.index("function bootstrap_runtime_dependencies {")]
  # Simulate device markers without touching the host filesystem.
  functions = functions.replace("/TICI", "./mock_tici").replace("/AGNOS", "./mock_agnos")
  if device:
    (tmp_path / "mock_agnos").touch()
  mocks = r'''
DIR="$PWD/project with spaces"
PYDEPS="$DIR/pydeps"
installed="$MOCK_INSTALLED"
pip_ready=0
function command {
  if [[ "$*" == "-v uv" && "$MOCK_INSTALLER" != uv ]]; then return 1; fi
  builtin command "$@"
}
function fake_install {
  printf 'INSTALL:'
  printf ' <%s>' "$@"
  printf '\n'
  if [[ " $* " == *" --no-index "* ]]; then
    [[ "$MOCK_OFFLINE_FAIL" == 1 ]] && return 1
  else
    [[ "$MOCK_ONLINE_FAIL" == 1 ]] && return 1
  fi
  installed=1
  return 0
}
function python3 {
  if [[ "$1" == -c ]]; then
    [[ "$installed" == 1 && "$MOCK_BROKEN_IMPORT" != 1 ]]
  elif [[ "$*" == '-m pip --version' ]]; then
    [[ "$MOCK_INSTALLER" == pip || "$pip_ready" == 1 ]]
  elif [[ "$1 $2" == '-m ensurepip' ]]; then
    echo ENSUREPIP
    [[ "$MOCK_INSTALLER" == ensurepip ]] || return 1
    pip_ready=1
  elif [[ "$1 $2 $3" == '-m pip install' ]]; then
    fake_install pip "$@"
  else
    echo "Unexpected Python invocation: $*" >&2
    return 99
  fi
}
function uv { fake_install uv "$@"; }
'''
  script = tmp_path / "test_install.sh"
  script.write_text(mocks + functions + '\nensure_python_package serial pyserial "$MOCK_REQUIRED" "$MOCK_DEPENDENCIES"\n',
                    encoding="utf-8", newline="\n")
  env = dict(os.environ, MOCK_INSTALLER=installer, MOCK_OFFLINE_FAIL=str(int(offline_fail)), MOCK_ONLINE_FAIL=str(int(online_fail)),
             MOCK_INSTALLED=str(int(installed)), MOCK_BROKEN_IMPORT=str(int(broken_import)), MOCK_REQUIRED=str(int(required)),
             MOCK_DEPENDENCIES=str(int(dependencies)))
  return subprocess.run([_bash(), script.name], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)


def test_existing_package_needs_no_installer(tmp_path):
  result = _install(tmp_path, installed=True, installer="none")
  assert result.returncode == 0, result.stderr
  assert "INSTALL:" not in result.stdout
  assert "ENSUREPIP" not in result.stdout


@pytest.mark.parametrize("installer", ["pip", "uv", "ensurepip"])
def test_missing_package_installs_from_wheel(tmp_path, installer):
  result = _install(tmp_path, installer=installer)
  assert result.returncode == 0, result.stderr
  assert result.stdout.count("INSTALL:") == 1
  assert "<--no-index>" in result.stdout
  assert "<--no-deps>" in result.stdout
  assert "project with spaces/pydeps>" in result.stdout
  assert "pyserial installed." in result.stdout
  if installer == "uv":
    assert "<--python> <python3> <--no-python-downloads>" in result.stdout
  if installer == "ensurepip":
    assert "ENSUREPIP" in result.stdout


@pytest.mark.parametrize("installer", ["pip", "uv"])
def test_ubuntu_retries_incompatible_wheel_online(tmp_path, installer):
  result = _install(tmp_path, installer=installer, offline_fail=True)
  assert result.returncode == 0, result.stderr
  installs = [line for line in result.stdout.splitlines() if line.startswith("INSTALL:")]
  assert len(installs) == 2
  assert "<--no-index>" in installs[0]
  assert "<--no-index>" not in installs[1]
  assert "<--no-deps>" not in installs[1]
  assert "pyserial installed." in result.stdout


def test_local_install_can_resolve_dependencies(tmp_path):
  result = _install(tmp_path, dependencies=True)
  assert result.returncode == 0, result.stderr
  assert "<--no-deps>" not in result.stdout


def test_device_never_retries_online(tmp_path):
  result = _install(tmp_path, offline_fail=True, device=True)
  assert result.returncode == 1
  assert result.stdout.count("INSTALL:") == 1
  assert "online package index" not in result.stdout


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("failure", ["install", "import", "installer"])
def test_failed_repair_blocks_only_required_packages(tmp_path, required, failure):
  result = _install(tmp_path, required=required, offline_fail=failure == "install", online_fail=failure == "install",
                    broken_import=failure == "import", installer="none" if failure == "installer" else "pip")
  assert result.returncode == int(required), result.stderr
  assert "pyserial installed." not in result.stdout
  assert ("not starting openpilot" if required else "continuing without it") in result.stdout


@pytest.fixture(scope="module")
def isolated_python(tmp_path_factory):
  venv = tmp_path_factory.mktemp("agnos_updater") / "venv"
  subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=60)
  return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.mark.parametrize(("version", "slot_verified", "pv5", "serial_state"), [
  ("old", False, True, "missing"),
  ("old", False, True, "installed"),
  ("old", False, True, "unavailable"),
  ("old", False, False, "unavailable"),
  ("old", True, True, "unavailable"),
  ("current", False, True, "unavailable"),
])
def test_agnos_updater_prepares_serial_only_for_pv5(tmp_path, isolated_python, version, slot_verified, pv5, serial_state):
  root = Path(BASEDIR)
  source = (root / "launch_chffrplus.sh").read_text(encoding="utf-8")
  installers = source[source.index("function install_runtime_python_package {"):source.index("function bootstrap_runtime_dependencies {")]
  agnos_init = source[source.index("function agnos_init {"):source.index("function start_carrot_recovery {")]
  # Execute the real AGNOS branch with fixtures for the OS files and device commands.
  installers = installers.replace("/TICI", "./mock_tici").replace("/AGNOS", "./mock_agnos")
  agnos_init = (agnos_init.replace("/VERSION", "./mock_version")
                .replace("/sys/firmware/devicetree/base/model", "./mock_model")
                .replace("/data/agnos-updater-ui.log", "updater.log")
                .replace("/data/agnos-update-confirmed", "confirmed"))
  (tmp_path / "mock_agnos").touch()
  (tmp_path / "mock_version").write_text(version, encoding="ascii")
  (tmp_path / "mock_model").write_text("comma c3x", encoding="ascii")
  (tmp_path / "confirmed").touch()
  project = tmp_path / "project with spaces"
  pydeps = project / "pydeps"
  pydeps.mkdir(parents=True)
  wheels = project / "third_party/wheels"
  wheels.mkdir(parents=True)
  wheel = root / "third_party/wheels/pyserial-3.5-py2.py3-none-any.whl"
  if serial_state != "unavailable":
    shutil.copy2(wheel, wheels / wheel.name)
  env = {**os.environ, "BOOTSTRAP_PYTHON": isolated_python.as_posix(), "BOOTSTRAP_ROOT": project.as_posix(),
         "PYTHONPATH": str(pydeps), "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1",
         "MOCK_SLOT_VERIFIED": str(int(slot_verified)), "MOCK_PV5": str(int(pv5))}
  assert subprocess.run([str(isolated_python), "-c", "import serial"], env=env, capture_output=True, timeout=10).returncode != 0
  if serial_state == "installed":
    subprocess.run([str(isolated_python), "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(pydeps), str(wheel)],
                   env=env, check=True, capture_output=True, timeout=30)

  mocks = r'''
DIR="$BOOTSTRAP_ROOT"
PYDEPS="$DIR/pydeps"
AGNOS_VERSION=current
unset AGNOS_UPDATE_CONFIRMATION_FILE
sudo() {
  if [[ "$1" == reboot ]]; then
    echo REBOOT
    exit 73
  fi
}
sleep() { :; }
run_startup_command() { "$@"; }
show_startup_failure() { echo "STARTUP_RECOVERY: $*"; }
show_agnos_update_failure() { echo "AGNOS_RECOVERY: $*"; }
python3() {
  case "$1" in
    */agnos.py) [[ "$2" == --verify ]] || exit 99; echo VERIFY; [[ "$MOCK_SLOT_VERIFIED" == 1 ]] ;;
    */pv5_build_recovery.py) echo CAR_IDENTITY; [[ "$MOCK_PV5" == 1 ]] ;;
    */updater.py)
      echo UPDATER
      if [[ "$MOCK_PV5" == 1 ]]; then
        "$BOOTSTRAP_PYTHON" -c 'import serial; assert serial.VERSION == "3.5"; print("UPDATER_IMPORT_OK")'
      fi
      ;;
    -c)
      if [[ "$2" == 'import jeepney' ]]; then return 0; fi
      "$BOOTSTRAP_PYTHON" "$@"
      ;;
    -m) "$BOOTSTRAP_PYTHON" "$@" ;;
    *) exit 99 ;;
  esac
}
'''
  script = tmp_path / "startup.sh"
  script.write_text(mocks + installers + agnos_init + "\nagnos_init\n", encoding="utf-8", newline="\n")
  result = subprocess.run([_bash(), script.name], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
  lines = result.stdout.splitlines()
  assert "online package index" not in result.stdout
  if version == "current":
    assert result.returncode == 0 and not lines, result.stdout + result.stderr
    assert not (tmp_path / "confirmed").exists()
  elif slot_verified:
    assert result.returncode == 73, result.stdout + result.stderr
    assert lines == ["AGNOS_PY=" + project.as_posix() + "/openpilot/system/hardware/tici/agnos.py",
                     "MANIFEST=" + project.as_posix() + "/openpilot/system/hardware/tici/agnos.json",
                     "MODEL=c3x", "VERIFY", "Verified AGNOS update activated; rebooting.", "REBOOT"]
  else:
    assert result.returncode == 1, result.stdout + result.stderr
    assert lines.index("VERIFY") < lines.index("CAR_IDENTITY")
    if not pv5:
      assert lines.count("UPDATER") == 3 and "pyserial" not in result.stdout
    elif serial_state == "unavailable":
      assert "STARTUP_RECOVERY: AGNOS updater dependency pyserial is unavailable" in lines
      assert "UPDATER" not in lines
    else:
      assert lines.count("UPDATER_IMPORT_OK") == 3, result.stdout + result.stderr
      prepared = "pyserial already installed." if serial_state == "installed" else "pyserial installed."
      assert lines.index("CAR_IDENTITY") < lines.index(prepared) < lines.index("UPDATER")
      assert ("pyserial installing from local wheel." in lines) == (serial_state == "missing")
