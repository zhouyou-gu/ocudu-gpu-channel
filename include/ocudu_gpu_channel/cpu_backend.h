#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include "ocudu_gpu_channel/processing.h"
#include <random>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

// CPU channel-processing backend — the reference implementation. It is not the
// product target (the CUDA backend is); it exists for local development on
// machines without a GPU, as the correctness reference the GPU backend is
// tested against, and as the fallback for model steps not yet ported to CUDA
// (AWGN, integer and fractional delay).

namespace ocg {

class CpuChannelProcessor final : public ChannelProcessor {
public:
  void prepare(const TopologyConfig& config) override;

  void process_into(const std::string& link_key,
                    const ModelConfig& model,
                    std::span<const IqSample> input,
                    std::span<IqSample> output,
                    std::uint64_t sample_rate_hz) override;

  ProcessorTimings last_timings() const override { return {}; }
  const char* backend_name() const override { return "cpu"; }

  // Convenience wrapper for single-shot use (tests, local development).
  IqBuffer process(const std::string& link_key,
                   const ModelConfig& model,
                   const IqBuffer& input,
                   std::uint64_t sample_rate_hz);

private:
  // Per-model-step running state (CFO phase, delay history, AWGN RNG).
  struct StepState {
    std::vector<IqSample> delay_line;
    double phase_rad = 0.0;
    std::mt19937 rng;
    std::normal_distribution<float> noise; // reused across batches; sigma passed per call
  };

  // All state owned by one link: two ping-pong scratch buffers and one
  // StepState per model-chain step. A link is processed by a single thread,
  // so a LinkState needs no internal locking.
  struct LinkState {
    IqBuffer scratch_a;
    IqBuffer scratch_b;
    std::vector<StepState> steps;
  };

  LinkState& ensure_link_state(const std::string& link_key,
                               const ModelConfig& model,
                               std::size_t sample_count);

  std::unordered_map<std::string, LinkState> states_;
};

} // namespace ocg
