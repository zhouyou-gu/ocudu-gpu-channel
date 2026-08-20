#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/processing.h"
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>

namespace ocg {

struct BrokerStats {
  std::uint64_t tx_pulls = 0;
  std::uint64_t rx_requests = 0;
  std::uint64_t rx_starvations = 0;
  std::uint64_t tx_queue_overflows = 0;
  std::uint64_t tx_sequence_gaps = 0;
  std::uint64_t zmq_errors = 0;
};

// Bounded wire-boundary capture, off unless a directory is set.
//
// Each port records the first `samples_per_port` IQ samples it pulled off its
// peer's TX, and the first it replied with on its own RX, writing both once at
// shutdown. Nothing is recorded from inside the channel call: these are the two
// wires, so an independent checker can verify that what left the broker is the
// DECLARED matrix applied to what entered it. Judging a matrix from the
// broker's own accounting would only restate the broker's opinion of itself.
//
// The buffers are preallocated to their limit and each is touched by exactly
// one thread (the port's puller, the port's REP worker), so the capture adds a
// bounds check and a copy to each wire operation and no allocation or I/O.
struct WireCaptureConfig {
  std::string directory;             // empty = capture disabled
  std::size_t samples_per_port = 0;  // per port, per direction
  // Samples to let past before recording starts, per port per direction. A
  // radio's first samples are its ramp-up -- an OCUDU gNB emits silence until
  // its lower PHY is radiating -- and a window of silence measures nothing.
  // Both directions skip the same count, so a captured output row still lines
  // up sample-for-sample with the captured input columns.
  std::size_t skip_samples = 0;
};

class Broker {
public:
  explicit Broker(TopologyConfig config);

  // Must be called before run(); ignored once the workers are up.
  void set_wire_capture(WireCaptureConfig capture);

  BrokerStats run(std::chrono::milliseconds duration);

  // Phase 3 C3: expose per-link BrokerLinkControl pointers so the ControlServer
  // can resolve link_ids in incoming REQs. Forwarded from the underlying
  // ChannelProcessor. Map ownership is transferred to the caller; pointers
  // remain valid for the life of this Broker.
  std::unordered_map<std::string, BrokerLinkControl*> collect_control_links();

private:
  TopologyConfig config_;
  WireCaptureConfig capture_;
  std::unique_ptr<ChannelProcessor> processor_;
};

} // namespace ocg
