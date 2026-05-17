#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/processing.h"
#include <chrono>
#include <cstdint>
#include <memory>

namespace ocg {

struct BrokerStats {
  std::uint64_t tx_pulls = 0;
  std::uint64_t rx_requests = 0;
  std::uint64_t rx_starvations = 0;
  std::uint64_t tx_queue_overflows = 0;
  std::uint64_t tx_sequence_gaps = 0;
  std::uint64_t zmq_errors = 0;
};

class Broker {
public:
  explicit Broker(TopologyConfig config);

  BrokerStats run(std::chrono::milliseconds duration);

private:
  TopologyConfig config_;
  std::unique_ptr<ChannelProcessor> processor_;
};

} // namespace ocg
