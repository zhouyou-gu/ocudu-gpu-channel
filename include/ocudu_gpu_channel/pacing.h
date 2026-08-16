#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>

namespace ocg {

// Real-time pacer for a producer that feeds a lock-step ZMQ radio.
//
// A ZMQ radio has no clock of its own: it advances only as fast as the broker
// exchanges samples with it, so the broker is its clock and must not hand it
// samples faster than its nominal sample rate. Feeding faster is not merely
// wasteful, it is fatal on a multi-port radio. OCUDU's RX channel pushes each
// received message into a fixed circular buffer and, when that buffer is full,
// retries in place with a 1 us sleep (`radio_zmq_rx_channel.cpp`,
// `receive_response()`), on the single `radio` worker thread that serves every
// TX and RX channel of the session (`worker_manager.cpp`, `create_prio_worker`
// -> `single_worker`). One port's full buffer therefore captures the thread its
// sibling ports need, and `radio_zmq_rx_stream::receive()` pops the ports in
// sequence, so the PHY blocks on the starved sibling and never drains the full
// one. On a single-port radio the same retry loop is self-clearing back
// pressure; from two ports up it is a hard deadlock.
//
// A pacer that carries its lateness cannot be used against such a radio. The
// producer is blocked for as long as the radio takes to start requesting, and a
// catch-up pacer converts that idle period into the right to run flat out
// afterwards -- which is exactly the burst that fills the radio's buffer. This
// pacer bounds the lateness it will carry: debt beyond `max_debt` is dropped
// rather than spent as a burst, so the amount that can ever leave without a
// wait is bounded by `max_debt` worth of samples regardless of how long the
// producer was blocked.
//
// The pacer takes `now` as an argument and returns the deadline instead of
// sleeping, so its schedule is testable against a synthetic clock.
class RealTimePacer {
public:
  using Clock = std::chrono::steady_clock;

  RealTimePacer(std::uint64_t sample_rate_hz, Clock::duration max_debt)
      : rate_hz_(sample_rate_hz == 0 ? 1 : sample_rate_hz), max_debt_(max_debt)
  {
  }

  // Returns the instant the caller must wait for before publishing `count`
  // samples, and charges those samples to the schedule. A returned time point
  // at or before `now` means no wait is due. The first call anchors the
  // schedule to `now` and is never made to wait.
  Clock::time_point charge(Clock::time_point now, std::size_t count)
  {
    if (!anchored_) {
      deadline_ = now;
      anchored_ = true;
    } else if (now > deadline_ + max_debt_) {
      // Lateness beyond one bound is not recoverable at real time. Dropping it
      // keeps the pacer honest about the rate; carrying it would licence a
      // burst proportional to however long the producer was blocked.
      deadline_ = now - max_debt_;
    }
    const Clock::time_point due = deadline_;
    deadline_ += duration_for(count);
    return due;
  }

  // Wall time that `count` samples occupy at the paced rate.
  std::chrono::nanoseconds duration_for(std::size_t count) const
  {
    return std::chrono::nanoseconds(
        (static_cast<std::uint64_t>(count) * 1000000000ULL) / rate_hz_);
  }

private:
  std::uint64_t rate_hz_;
  Clock::duration max_debt_;
  Clock::time_point deadline_{};
  bool anchored_ = false;
};

} // namespace ocg
