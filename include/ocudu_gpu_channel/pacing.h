#pragma once

#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstddef>
#include <cstdint>

namespace ocg {

// Real-time pacer for a producer that feeds a lock-step ZMQ radio.
//
// The nominal deadline never rebases: a temporary delay remains eligible for
// recovery. A separate rate limiter bounds how quickly the producer can catch
// up. Only that limiter drops unused credit after an idle period; the nominal
// clock retains every sample and every elapsed interval.
//
// This distinction matters for OCUDU's shared radio worker: a full RX buffer
// retries in place and can block sibling ports. The limiter grants at most
// `burst_budget` of immediately spendable credit, independently of the length
// of a stall, and sustained recovery is at most recovery_ppm above nominal.
// Broker output headroom and common-window checks remain required. This is a
// bounded release envelope, not proof that an arbitrary peer meets deadlines.
//
// Pair each charge with published(actual_time), including after a late wake.
// Accounting only at charge time would let an overdue sleeping publication
// escape the release budget. Clock arguments keep the schedule testable.
class RealTimePacer {
public:
  using Clock = std::chrono::steady_clock;

  RealTimePacer(std::uint64_t sample_rate_hz, Clock::duration burst_budget,
                std::uint32_t recovery_ppm = 10000)
      : rate_hz_(sample_rate_hz == 0 ? 1 : sample_rate_hz), burst_budget_(burst_budget),
        recovery_denominator_(1000000ULL + recovery_ppm)
  {
    // Express the credit in limiter time, so faster recovery cannot enlarge
    // the caller's bound on represented signal time released without waiting.
    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(burst_budget).count();
    burst_budget_ = std::chrono::nanoseconds((ns / recovery_denominator_) * 1000000ULL +
                                            ((ns % recovery_denominator_) * 1000000ULL) / recovery_denominator_);
  }

  // Returns the instant the caller must wait for before publishing `count`
  // samples, and charges those samples to the schedule. A returned time point
  // at or before `now` means no wait is due. The first call anchors the
  // schedule to `now` and is never made to wait.
  Clock::time_point charge(Clock::time_point now, std::size_t count)
  {
    assert(!publication_pending_ && count > 0);
    if (!anchored_) {
      nominal_deadline_ = limiter_deadline_ = now;
      anchored_ = true;
    }
    limiter_deadline_ = std::max(limiter_deadline_, now - burst_budget_);
    const auto due = std::max(nominal_deadline_, limiter_deadline_);

    // Carry fractions between fragments instead of rounding away part of a
    // sample on every call. Divide whole seconds first to avoid long-run
    // overflow from multiplying a cumulative sample count by 1e9.
    const auto fraction = (count % rate_hz_) * 1000000000ULL + nominal_remainder_;
    const auto nominal_ns = (count / rate_hz_) * 1000000000ULL + fraction / rate_hz_;
    nominal_remainder_ = fraction % rate_hz_;
    nominal_deadline_ += std::chrono::nanoseconds(nominal_ns);
    const auto recovery_fraction = (nominal_ns % recovery_denominator_) * 1000000ULL + recovery_remainder_;
    const auto recovery_ns = (nominal_ns / recovery_denominator_) * 1000000ULL +
                             recovery_fraction / recovery_denominator_;
    recovery_remainder_ = recovery_fraction % recovery_denominator_;
    charged_recovery_duration_ = std::chrono::nanoseconds(recovery_ns);
    limiter_deadline_ += charged_recovery_duration_;
    publication_pending_ = true;
    return due;
  }

  void published(Clock::time_point now)
  {
    assert(publication_pending_);
    // The current publication consumes credit even if its preceding wait
    // overslept. It must not be followed by a fresh, uncharged recovery burst.
    limiter_deadline_ = std::max(limiter_deadline_, now - burst_budget_ + charged_recovery_duration_);
    lag_ = std::max(Clock::duration::zero(), now - nominal_deadline_);
    max_lag_ = std::max(max_lag_, lag_);
    publication_pending_ = false;
  }

  Clock::duration lag() const { return lag_; }
  Clock::duration max_lag() const { return max_lag_; }

  // Wall time that `count` samples occupy at the paced rate.
  std::chrono::nanoseconds duration_for(std::size_t count) const
  {
    return std::chrono::nanoseconds(
        (count / rate_hz_) * 1000000000ULL + ((count % rate_hz_) * 1000000000ULL) / rate_hz_);
  }

private:
  std::uint64_t rate_hz_;
  Clock::duration burst_budget_;
  std::uint64_t recovery_denominator_;
  std::uint64_t nominal_remainder_ = 0;
  std::uint64_t recovery_remainder_ = 0;
  Clock::time_point nominal_deadline_{};
  Clock::time_point limiter_deadline_{};
  Clock::duration charged_recovery_duration_{};
  Clock::duration lag_{};
  Clock::duration max_lag_{};
  bool anchored_ = false;
  bool publication_pending_ = false;
};

} // namespace ocg
