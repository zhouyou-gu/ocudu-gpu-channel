#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/iq.h"
#include "ocudu_gpu_channel/processing.h"
#include <array>
#include <random>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

// CPU channel-processing backend — the reference implementation. It is not the
// product target (the CUDA backend is); it exists for local development on
// machines without a GPU, as the correctness reference the GPU backend is
// tested against, and as the fallback for model steps not yet ported to CUDA
// (integer and fractional delay).

namespace ocg {

class CpuChannelProcessor final : public ChannelProcessor {
public:
  void prepare(const TopologyConfig& config) override;

  void process_superposition(const std::string& dst_key,
                             const std::vector<SuperpositionInput>& inputs,
                             const ModelConfig* rx_model,
                             std::uint64_t sample_rate_hz,
                             std::span<IqSample> output) override;

  ProcessorTimings last_timings() const override { return {}; }
  const char* backend_name() const override { return "cpu"; }

private:
  // Per-model-step running state (CFO phase, delay history, AWGN RNG).
  // Fat-struct pattern: one struct serves every step type; the field set used
  // depends on `step.type` at runtime. `delay_line` is shared by IntegerDelay /
  // FractionalDelay / Tdl steps -- for Tdl it is the single cross-slot history
  // ring sized to `ceil(max tau_k) + kTdlFracFilterTaps` at prepare time. The
  // tdl_* fields are populated only when this step is a Tdl step.
  struct StepState {
    std::vector<IqSample> delay_line;
    double phase_rad = 0.0;
    std::mt19937 rng;
    std::normal_distribution<float> noise; // reused across batches; sigma passed per call
    std::vector<TapSpec> tdl_taps;  // cached copy of ModelStep::taps for the Tdl kernel
    // Per-tap precomputed 8-tap Hamming-windowed sinc coefficients for the
    // fractional offset frac_k = tau_k - floor(tau_k). Integer-only taps still
    // get a coefficient set so the kernel runs the same inner loop for every
    // tap (sinc(frac=0) collapses to a unit impulse at index 3).
    std::vector<std::array<float, kTdlFracFilterTaps>> tdl_polyphase;
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

  // One-shot setup for a Tdl step's per-link runtime state. Called from
  // ensure_link_state whenever a step in the chain is a Tdl step. Static
  // because it owns no instance state -- the StepState reference carries
  // everything it needs to populate.
  static void prepare_tdl_step(StepState& state, const ModelStep& step);

  // Internal helper used by process_superposition() to shape one edge's
  // input through its model chain into the provided output span. This is
  // what process_into() used to be -- now private since the public API only
  // exposes the per-node superposition entry point.
  void apply_chain_to_link(const std::string& link_key,
                           const ModelConfig& model,
                           std::span<const IqSample> input,
                           std::span<IqSample> output,
                           std::uint64_t sample_rate_hz);

  std::unordered_map<std::string, LinkState> states_;
};

} // namespace ocg
