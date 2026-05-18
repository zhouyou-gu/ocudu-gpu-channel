#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include <memory>
#include <span>
#include <string>
#include <vector>

// Channel-processing backend interface and selection.
//
// The emulator has two backends, each in its own translation unit:
//   - cuda_backend (cuda_backend.h / .cu): the PRIMARY target — GPU-accelerated
//     channel emulation built to scale to many concurrent gNB/UE links.
//   - cpu_backend  (cpu_backend.h / .cpp): the reference/fallback backend.
//
// Both implement the ChannelProcessor interface below. create_channel_processor()
// picks one from the topology's runtime.backend (CUDA by default).

namespace ocg {

struct ProcessorTimings {
  double h2d_us = 0.0;
  double kernel_us = 0.0;
  double d2h_us = 0.0;
  double gpu_process_us = 0.0;
};

class ChannelProcessor {
public:
  virtual ~ChannelProcessor() = default;

  // prepare() must be called once for the whole topology before process_into().
  // It preallocates all per-link state, so process_into() can then be called
  // concurrently from different threads as long as each thread uses a distinct
  // link_key (one server thread per destination device, in the broker).
  virtual void prepare(const TopologyConfig& config) = 0;

  virtual void process_into(const std::string& link_key,
                            const ModelConfig& model,
                            std::span<const IqSample> input,
                            std::span<IqSample> output,
                            std::uint64_t sample_rate_hz) = 0;

  virtual ProcessorTimings last_timings() const = 0;
  virtual const char* backend_name() const = 0;
};

// Returns the model steps in `config` the CUDA backend cannot run yet (AWGN and
// delay); empty when the topology is fully GPU-supported.
std::vector<std::string> validate_cuda_support(const TopologyConfig& config);

// Builds the processor for config.runtime.backend (Backend::Cuda by default).
std::unique_ptr<ChannelProcessor> create_channel_processor(const TopologyConfig& config);

} // namespace ocg
