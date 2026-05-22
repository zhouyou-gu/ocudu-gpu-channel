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

// One incoming edge of a superposition: the link's channel model and the
// source node's current TX batch. `samples` is borrowed for the call only.
struct SuperpositionInput {
  std::string link_key;
  const ModelConfig* model = nullptr;
  std::span<const IqSample> samples;
};

class ChannelProcessor {
public:
  virtual ~ChannelProcessor() = default;

  // prepare() must be called once for the whole topology before
  // process_superposition(). It preallocates all per-link and per-node state,
  // so process_superposition() can then be called concurrently from different
  // threads as long as each thread uses a distinct dst_key (one server thread
  // per destination device, in the broker).
  virtual void prepare(const TopologyConfig& config) = 0;

  // Processes every incoming edge of destination node `dst_key` through its
  // own channel model, sums them, and applies the node's optional receiver
  // model `rx_model` (a thermal-noise floor) once to the sum -- writing the
  // node's received signal (desired + interference + crosstalk + noise) into
  // `output`. `rx_model` may be null. On the CUDA backend the per-edge
  // shaping, the summation, and the receiver model all run on the GPU in a
  // single fused launch. Single-edge processing is just the N=1 case: pass a
  // one-element inputs list with rx_model = nullptr.
  virtual void process_superposition(const std::string& dst_key,
                                     const std::vector<SuperpositionInput>& inputs,
                                     const ModelConfig* rx_model,
                                     std::uint64_t sample_rate_hz,
                                     std::span<IqSample> output) = 0;

  virtual ProcessorTimings last_timings() const = 0;
  virtual const char* backend_name() const = 0;
};

// Returns the model steps in `config` the CUDA backend cannot run yet; empty
// when the topology is fully GPU-supported. The legacy gap was integer /
// fractional delay (now CUDA-supported); the open gap is `tdl` until Phase 1.1
// lands the multi-tap kernel.
std::vector<std::string> validate_cuda_support(const TopologyConfig& config);

// Same idea for the CPU backend: returns the model steps the CPU chain loop
// has no handler for. Open gap is `tdl` until Phase 1.1 lands the reference
// multi-tap implementation. Exposed so external builders of TopologyConfig can
// check up front rather than discovering the gap on first slot.
std::vector<std::string> validate_cpu_support(const TopologyConfig& config);

// Builds the processor for config.runtime.backend (Backend::Cuda by default).
std::unique_ptr<ChannelProcessor> create_channel_processor(const TopologyConfig& config);

} // namespace ocg
