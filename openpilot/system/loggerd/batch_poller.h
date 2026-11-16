#pragma once

#include <algorithm>
#include <chrono>
#include <cerrno>
#include <csignal>
#include <pthread.h>
#include <sys/select.h>
#include <system_error>
#include <vector>

#include "msgq/impl_msgq.h"

// Logger-only opt-in. Other subscribers retain Poller::poll's existing behavior.
class LoggerdBatchPoller {
public:
  using Clock = std::chrono::steady_clock;
  static constexpr auto interval = std::chrono::milliseconds(10);

  explicit LoggerdBatchPoller(Poller &poller) : poller_(poller), next_scan_(Clock::now()) {
#ifdef __linux__
    // Fake/alternate transports and macOS do not use Linux's thread SIGUSR2 wake.
    signal_notifications_ = dynamic_cast<MSGQPoller *>(&poller_) != nullptr;
#endif
  }

  template <typename Cancelled>
  std::vector<SubSocket *> poll(int timeout_ms, Cancelled cancelled) {
    if (cancelled()) return {};
    if (!signal_notifications_ || timeout_ms == 0) return poller_.poll(timeout_ms);
    if (timeout_ms < -1) throw std::system_error(EINVAL, std::generic_category(), "logger poll timeout");

    // Respect a caller that already disabled notification delivery. In that
    // uncommon configuration retain the original timeout/scan behavior, without
    // changing its exit-signal mask around the blocking backend call.
    sigset_t original;
    const int mask_error = pthread_sigmask(SIG_SETMASK, nullptr, &original);
    if (mask_error) throw std::system_error(mask_error, std::generic_category(), "logger signal mask query");
    if (sigismember(&original, SIGUSR2)) return poller_.poll(timeout_ms);

    const auto deadline = timeout_ms < 0 ? Clock::time_point::max() : Clock::now() + std::chrono::milliseconds(timeout_ms);
    SignalMask mask;
    while (!cancelled()) {
      auto now = Clock::now();
      if (now >= deadline) return {};
      if (now < next_scan_) {
        // Leave notification pending/coalesced, but atomically unblock exit
        // signals during the wait so cancellation cannot fall into a lost-wake gap.
        wait_until(std::min(next_scan_, deadline), mask.batch_wait_mask());
        continue;
      }
      // Start-to-start spacing: processing/draining counts toward this interval.
      next_scan_ = now + interval;
      auto ready = poller_.poll(0);
      if (!ready.empty() || cancelled()) return ready;
      // SIGUSR2 is still blocked across the empty scan. pselect restores the
      // caller's mask atomically as it waits, including an intervening publish.
      wait_until(deadline, mask.original());
    }
    return {};
  }

private:
  class SignalMask {
  public:
    SignalMask() {
      sigset_t blocked;
      sigemptyset(&blocked);
      sigaddset(&blocked, SIGUSR2);
      sigaddset(&blocked, SIGINT);
      sigaddset(&blocked, SIGTERM);
#ifdef SIGPWR
      sigaddset(&blocked, SIGPWR);
#endif
      const int err = pthread_sigmask(SIG_BLOCK, &blocked, &original_);
      if (err) throw std::system_error(err, std::generic_category(), "logger signal mask");
      batch_wait_ = original_;
      sigaddset(&batch_wait_, SIGUSR2);
    }
    ~SignalMask() { pthread_sigmask(SIG_SETMASK, &original_, nullptr); }
    const sigset_t *original() const { return &original_; }
    const sigset_t *batch_wait_mask() const { return &batch_wait_; }
  private:
    sigset_t original_, batch_wait_;
  };

  static void wait_until(Clock::time_point deadline, const sigset_t *mask) {
    auto remaining = deadline == Clock::time_point::max() ? std::chrono::seconds(1) : deadline - Clock::now();
    if (remaining <= Clock::duration::zero()) return;
    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(remaining).count();
    const timespec timeout = {static_cast<time_t>(ns / 1000000000), static_cast<long>(ns % 1000000000)};
    const int ret = pselect(0, nullptr, nullptr, nullptr, &timeout, mask);
    if (ret < 0 && errno != EINTR) throw std::system_error(errno, std::generic_category(), "logger notification wait");
  }

  Poller &poller_;
  bool signal_notifications_ = false;
  Clock::time_point next_scan_;
};
