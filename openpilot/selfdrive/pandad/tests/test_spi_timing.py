"""SPI clock-domain regression; the native peer is simulated, never a real device."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SPI = ROOT / "openpilot/selfdrive/pandad/spi.cc"


def test_spi_timing_clock_domains_and_protocol_constants():
  source = SPI.read_text()
  assert "while ((nanos_monotonic() - start_ns) < turnaround_ns) {}" in source
  assert "wait_for_spi_turnaround(spi_last_bus_activity_mono_ns," in source
  assert "wait_for_spi_turnaround(nanos_monotonic(), SPI_PHASE_TURNAROUND_NS);" in source
  assert source.count("spi_last_bus_activity_mono_ns = nanos_monotonic();") == 2
  assert source.count("spi_last_bus_activity_ns = nanos_since_boot();") == 2
  assert "double start_millis = millis_since_boot();" in source
  assert "if (millis_since_boot() - start_millis > timeout)" in source
  assert "timeout = std::clamp(timeout, 20U, SPI_ACK_TIMEOUT);" in source
  assert "const unsigned int SPI_ACK_TIMEOUT = 500;" in source
  for name, value in (("SPI_PHASE_TURNAROUND_NS", 400000), ("SPI_CAN_INTER_TRANSACTION_NS", 400000),
                      ("SPI_CONTROL_INTER_TRANSACTION_NS", 1000000)):
    assert f"constexpr uint64_t {name} = {value}ULL;" in source
  # The external log/message timestamp domain remains BOOTTIME.
  timing = (ROOT / "openpilot/common/timing.h").read_text()
  boot = timing.split("static inline uint64_t nanos_since_boot()", 1)[1].split("}", 1)[0]
  assert "clock_gettime(CLOCK_BOOTTIME, &t);" in boot


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("g++") is None, reason="Linux C++ compiler required")
def test_spi_native_protocol_faults_and_clock_domains(tmp_path):
  output = tmp_path / "spi_timing"
  native_test = Path(__file__).with_suffix(".cc")
  command = ["g++", "-std=c++17", "-O2", "-DNDEBUG", "-DMONOTONIC_GUARD=1", f'-DSPI_SOURCE="{SPI}"',
             f"-I{ROOT}", f"-I{ROOT / 'openpilot'}", str(native_test), "-pthread", "-o", str(output)]
  command += [f"-Wl,--wrap={name}" for name in ("clock_gettime", "open", "close", "flock", "usleep")]
  subprocess.run(command, check=True, capture_output=True, timeout=60)
  result = subprocess.run([str(output)], check=True, capture_output=True, text=True, timeout=20)
  assert "passed_cases=31" in result.stderr
  assert "hot_loop_clock_reads boot=0 mono=40" in result.stderr
  assert "hot_loop_clock_reads boot=0 mono=100" in result.stderr
  assert "quiet_guard_resume_ns=1000000" in result.stderr
