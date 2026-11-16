// Standalone deterministic native test. Compile with SPI_SOURCE pointing to spi.cc.
// It executes the real SPI implementation with an in-memory protocol peer;
// no device, vehicle, CAN socket or real sleeping is used.
#undef NDEBUG
#include <sys/file.h>
#include <sys/ioctl.h>
#include <linux/spi/spidev.h>
#include <algorithm>
#include <cassert>
#include <cinttypes>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <cstdarg>
#include "common/util.h"
#include "common/swaglog.h"
#define private public
#include "selfdrive/pandad/panda_comms.h"
#undef private
#ifndef SPI_SOURCE
#define SPI_SOURCE "../spi.cc"
#endif
#include SPI_SOURCE

namespace sim {
enum class Phase { header, hack, data, dack, receive };
enum class Fault { none, header_io, hack_nack, dack_nack, checksum, hack_timeout, dack_timeout, length };
uint64_t now = 1000000000ULL, boot_offset = 123000000000ULL;
uint64_t boot_reads = 0, mono_reads = 0, last_end = 0, hack_ack = 0, minimum_next = 0;
uint64_t wire_hash = 1469598103934665603ULL, wire_calls = 0, recovery_calls = 0, header_calls = 0;
uint64_t io_step = 10000, direct_calls = 0, suspend_at = 0;
int direct_ack = -1;
bool direct = false, fault_used = false;
Fault fault = Fault::none;
Phase phase = Phase::header;
uint8_t endpoint = 0, request = 0;
uint16_t transmit_length = 0, maximum_receive = 0, receive_length = 0;
void hash(uint64_t value) { wire_hash ^= value; wire_hash *= 1099511628211ULL; }
void reset(Fault f = Fault::none) {
  now = 1000000000ULL; boot_offset = 123000000000ULL;
  boot_reads = mono_reads = last_end = hack_ack = minimum_next = 0;
  wire_hash = 1469598103934665603ULL; wire_calls = recovery_calls = header_calls = 0;
  phase = Phase::header; fault = f; fault_used = false; direct = false; direct_ack = -1;
  direct_calls = suspend_at = 0; io_step = 10000;
  spi_last_bus_activity_ns = now + boot_offset;
#ifdef MONOTONIC_GUARD
  spi_last_bus_activity_mono_ns = now;
#endif
  spi_next_transaction_ns = SPI_CAN_INTER_TRANSACTION_NS;
  for (auto &stats : spi_phase_diag) stats = {};
}
int ioctl(spi_ioc_transfer &t) {
  auto tx = reinterpret_cast<uint8_t *>(t.tx_buf);
  auto rx = reinterpret_cast<uint8_t *>(t.rx_buf);
  now += io_step;
  if (direct) {
    direct_calls++;
    if (suspend_at && direct_calls == suspend_at) boot_offset += 10000000000ULL;
    rx[0] = direct_ack < 0 ? 0 : static_cast<uint8_t>(direct_ack);
    return t.len;
  }
  wire_calls++; hash(t.len);
  for (unsigned i = 0; i < t.len; i++) hash(tx[i]);
  if (t.len == SPI_BUF_SIZE/2 && tx[0] == 0x14) {
    rx[0] = SPI_NACK; recovery_calls++; phase = Phase::header;
    fault_used = true; last_end = now; minimum_next = SPI_CAN_INTER_TRANSACTION_NS;
    return t.len;
  }
  switch (phase) {
    case Phase::header:
      assert(t.len == 7 && tx[0] == SPI_SYNC && check_checksum(tx, t.len));
      endpoint = tx[1]; std::memcpy(&transmit_length, tx + 2, 2); std::memcpy(&maximum_receive, tx + 4, 2);
      if (last_end) assert(now - last_end >= std::max(minimum_next, endpoint == 0 ? SPI_CONTROL_INTER_TRANSACTION_NS : SPI_CAN_INTER_TRANSACTION_NS));
      header_calls++; request = 0;
      if (fault == Fault::header_io && !fault_used) { fault_used = true; return -1; }
      phase = Phase::hack; return t.len;
    case Phase::hack:
      assert(t.len == 1 && tx[0] == 0x11);
      if (fault == Fault::hack_timeout && !fault_used) { rx[0] = 0; return t.len; }
      if (fault == Fault::hack_nack && !fault_used) { rx[0] = SPI_NACK; fault_used = true; return t.len; }
      rx[0] = SPI_HACK; hack_ack = now; phase = Phase::data; return t.len;
    case Phase::data:
      assert(t.len == transmit_length + 1U && check_checksum(tx, t.len));
      assert(now - hack_ack >= SPI_PHASE_TURNAROUND_NS);
      request = transmit_length && endpoint == 0 ? tx[0] : 0;
      receive_length = maximum_receive; phase = Phase::dack; return t.len;
    case Phase::dack:
      assert(t.len == 3 && tx[0] == 0x13);
      if (fault == Fault::dack_timeout && !fault_used) { rx[0] = 0; return t.len; }
      if (fault == Fault::dack_nack && !fault_used) { rx[0] = SPI_NACK; fault_used = true; return t.len; }
      rx[0] = SPI_DACK;
      if (fault == Fault::length && !fault_used) {
        uint16_t impossible = SPI_BUF_SIZE; std::memcpy(rx + 1, &impossible, 2); fault_used = true; return t.len;
      }
      std::memcpy(rx + 1, &receive_length, 2); phase = Phase::receive; return t.len;
    case Phase::receive: {
      assert(t.len == receive_length + 1U);
      uint8_t checksum = SPI_CHECKSUM_START ^ SPI_DACK ^ (receive_length & 0xff) ^ (receive_length >> 8);
      for (unsigned i = 0; i < receive_length; i++) { rx[i] = (i + 17) & 0xff; checksum ^= rx[i]; }
      rx[receive_length] = checksum;
      if (fault == Fault::checksum && !fault_used) { rx[receive_length] ^= 1; fault_used = true; }
      phase = Phase::header; last_end = now;
      minimum_next = request == 0xdc ? SPI_CONTROL_INTER_TRANSACTION_NS : SPI_CAN_INTER_TRANSACTION_NS;
      return t.len;
    }
  }
  throw std::runtime_error("unexpected simulated phase");
}
}
extern "C" int __wrap_clock_gettime(clockid_t id, timespec *out) {
  assert(id == CLOCK_BOOTTIME || id == CLOCK_MONOTONIC);
  const uint64_t ticks = sim::now + (id == CLOCK_BOOTTIME ? sim::boot_offset : 0);
  if (id == CLOCK_BOOTTIME) sim::boot_reads++; else sim::mono_reads++;
  out->tv_sec = ticks / 1000000000ULL; out->tv_nsec = ticks % 1000000000ULL;
  sim::now += 10000; return 0;
}
extern "C" int __wrap_open(const char *path, int, ...) { assert(std::string(path) == SPI_DEVICE); return 42; }
extern "C" int __wrap_close(int fd) { assert(fd == 42); return 0; }
extern "C" int __wrap_flock(int fd, int) { assert(fd == 42); return 0; }
extern "C" int __wrap_usleep(useconds_t us) { sim::now += static_cast<uint64_t>(us) * 1000; return 0; }
namespace util {
bool file_exists(const std::string &name) { return name == SPI_DEVICE; }
std::string getenv(const char *, std::string fallback) { return fallback; }
std::string hexdump(const uint8_t *, const size_t) { return "test"; }
int safe_ioctl(int fd, unsigned long request, void *arg, const char *) {
  assert(fd == 42);
  if (request != SPI_IOC_MESSAGE(1)) return 0;
  return sim::ioctl(*static_cast<spi_ioc_transfer *>(arg));
}
}
void cloudlog_e(int, const char *, int, const char *, const char *, ...) {}

int main() {
  sim::reset();
  PandaSpiHandle panda("");
  unsigned cases = 0;
  const auto print_case = [&](const char *name, int result) {
    std::cout << name << ' ' << result << ' ' << sim::wire_hash << ' ' << sim::wire_calls << ' ' << sim::recovery_calls << '\n';
    cases++;
  };
  uint8_t data[32] = {}, output[32] = {};
  for (auto fault : {sim::Fault::none, sim::Fault::header_io, sim::Fault::hack_nack, sim::Fault::dack_nack,
                     sim::Fault::checksum, sim::Fault::hack_timeout, sim::Fault::dack_timeout, sim::Fault::length}) {
    sim::reset(fault);
    int result = panda.bulk_read(0x81, output, 16, 20);
    assert(result == 16);
    for (int i = 0; i < 16; i++) assert(output[i] == i + 17);
    assert(sim::recovery_calls == (fault == sim::Fault::none ? 0 : 3));
    assert(spi_attempt_timing.total_us < 1000000);  // disjoint clocks cannot underflow durations
    print_case("receive_fault", result);
  }
  for (auto fault : {sim::Fault::none, sim::Fault::header_io, sim::Fault::hack_nack, sim::Fault::dack_nack,
                     sim::Fault::checksum, sim::Fault::hack_timeout, sim::Fault::dack_timeout, sim::Fault::length}) {
    sim::reset(fault);
    int result = panda.bulk_write(3, data, 8, 20);
    assert(result == 0);
    assert(sim::recovery_calls == (fault == sim::Fault::none ? 0 : 3));
    assert(spi_attempt_timing.total_us < 1000000);
    print_case("transmit_fault", result);
  }
  sim::reset();
  assert(panda.bulk_write(3, data, 8, 20) == 0);
  assert(panda.bulk_read(0x81, output, 16, 20) == 16);
  print_case("can_gap", 0);
  sim::reset();
  assert(panda.control_write(0xd2, 0, 0, 20) == 0);
  assert(panda.bulk_read(0x81, output, 16, 20) == 16);
  print_case("control_gap", 0);
  sim::reset();
  assert(panda.control_write(0xdc, 0, 0, 20) == 0);
  assert(panda.bulk_read(0x81, output, 16, 20) == 16);
  print_case("safety_mode_gap", 0);
  for (unsigned timeout : {0U, 1U, 19U, 20U, 500U, 501U}) {
    sim::reset(); sim::direct = true; sim::io_step = 1000000;
    const uint64_t started = sim::now;
    assert(panda.wait_for_ack(SPI_HACK, 0x11, timeout, 1) == SpiError::ACK_TIMEOUT);
    const uint64_t expected = static_cast<uint64_t>(std::clamp(timeout ? timeout : SPI_ACK_TIMEOUT, 20U, SPI_ACK_TIMEOUT)) * 1000000;
    assert(sim::now - started > expected && sim::now - started < expected + 2100000);
    std::cout << "timeout " << timeout << ' ' << sim::direct_calls << '\n'; cases++;
  }
  sim::reset(); sim::direct = true; sim::io_step = 1000000; sim::suspend_at = 3;
  assert(panda.wait_for_ack(SPI_DACK, 0x13, 500, 3) == SpiError::ACK_TIMEOUT);
  assert(sim::direct_calls == 3);  // suspend counts in ACK BOOTTIME deadline, as before
  print_case("ack_suspend", 0);
  sim::reset(); sim::direct = true; sim::direct_ack = SPI_NACK;
  assert(panda.wait_for_ack(SPI_DACK, 0x13, 500, 3) == SpiError::NACK);
  assert(sim::direct_calls == 1); print_case("immediate_nack", 0);
  sim::reset(); sim::direct = true; sim::direct_ack = SPI_HACK;
  assert(panda.wait_for_ack(SPI_HACK, 0x11, 500, 1) == 0);
  assert(sim::direct_calls == 1); print_case("immediate_ack", 0);
  for (const uint64_t interval : {400000ULL, 1000000ULL}) {
  sim::reset();
  const uint64_t real_start = sim::now;
#ifdef MONOTONIC_GUARD
  uint64_t started = nanos_monotonic();
#else
  uint64_t started = nanos_since_boot();
#endif
  const auto boot_before = sim::boot_reads, mono_before = sim::mono_reads;
  wait_for_spi_turnaround(started, interval);
  assert(sim::now - real_start >= interval && sim::now - real_start < interval + 20000);
#ifdef MONOTONIC_GUARD
  assert(sim::boot_reads == boot_before && sim::mono_reads > mono_before);
#else
  assert(sim::boot_reads > boot_before && sim::mono_reads == mono_before);
#endif
  std::cerr << "hot_loop_clock_reads boot=" << sim::boot_reads - boot_before << " mono=" << sim::mono_reads - mono_before << '\n';
  assert(nanos_since_boot() - nanos_monotonic() > 122000000000ULL); // shared timestamp domain unchanged
  cases++;
  }
  sim::reset();
#ifdef MONOTONIC_GUARD
  const uint64_t prior_activity = nanos_monotonic();
#else
  const uint64_t prior_activity = nanos_since_boot();
#endif
  sim::boot_offset += 10000000000ULL; // suspend advances BOOTTIME, not MONOTONIC
  const uint64_t resumed = sim::now;
  wait_for_spi_turnaround(prior_activity, SPI_CONTROL_INTER_TRANSACTION_NS);
#ifdef MONOTONIC_GUARD
  assert(sim::now - resumed >= SPI_CONTROL_INTER_TRANSACTION_NS);
  assert(sim::now - resumed <= SPI_CONTROL_INTER_TRANSACTION_NS + 10000);
#else
  assert(sim::now - resumed == 10000); // previous guard considers suspend time
#endif
  std::cerr << "quiet_guard_resume_ns=" << sim::now - resumed << '\n';
  cases++;
  std::cerr << "passed_cases=" << cases << '\n';
}
