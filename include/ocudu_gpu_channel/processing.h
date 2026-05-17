#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include <memory>
#include <random>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

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

std::vector<std::string> validate_cuda_support(const TopologyConfig& config);
std::unique_ptr<ChannelProcessor> create_channel_processor(const TopologyConfig& config);

} // namespace ocg
