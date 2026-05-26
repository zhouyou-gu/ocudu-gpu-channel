#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/iq.h"
#include "ocudu_gpu_channel/mutable_params.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/runtime_control.h"
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

  std::unordered_map<std::string, BrokerLinkControl*> collect_control_links() override;

private:
  // Per-model-step running state (CFO phase, delay history, AWGN RNG).
  // Fat-struct pattern: one struct serves every step type; the field set used
  // depends on `step.type` at runtime. `delay_line` is shared by IntegerDelay /
  // FractionalDelay / Tdl steps -- for Tdl it is the single cross-slot history
  // ring sized to `ceil(max tau_k) + kTdlFracFilterTaps` at prepare time.
  //
  // The Tdl kernel reads tap data (delay/gain/phase) directly from the live
  // `ModelStep::taps` in the owning `ModelConfig` -- there is no per-link
  // cached copy here. The only cached state is `tdl_polyphase`, the per-tap
  // 8-tap Hamming-windowed sinc coefficients derived once at prepare time
  // from each tap's fractional offset. Integer-only taps still get a
  // coefficient set so the inner kernel loop is identical for every tap
  // (sinc(frac=0) collapses to a unit impulse at index 3).
  struct StepState {
    std::vector<IqSample> delay_line;
    double phase_rad = 0.0;
    std::mt19937 rng;
    std::normal_distribution<float> noise; // reused across batches; sigma passed per call
    std::vector<std::array<float, kTdlFracFilterTaps>> tdl_polyphase;
    // Per-link state for the optional fading sub-config of a Tdl step.
    // Populated by prepare_tdl_fading_state when fading_enabled is true;
    // remains disabled (and unused) otherwise.
    TdlFadingState tdl_fading;
  };

  // All state owned by one link: two ping-pong scratch buffers and one
  // StepState per model-chain step. A link is processed by a single thread,
  // so a LinkState needs no internal locking.
  struct LinkState {
    IqBuffer scratch_a;
    IqBuffer scratch_b;
    std::vector<StepState> steps;
    // Runtime-mutable scalar params (Phase 3 v1). `live` is the canonical
    // source read by apply_chain_to_link (post-C2a). `ctl.shadow` is the
    // write target for the ZMQ control thread (Phase 3 C3+); the snap step
    // at the top of every serve copies shadow → live if ctl.seqno advanced
    // since the last snap. `live_seqno` tracks the version this LinkState
    // last consumed.
    MutableParams live;
    BrokerLinkControl ctl;
    std::uint32_t live_seqno = 0;

    // v2.0-F3: live profile-swap state. When `live_profile_active` is true,
    // the chain executor reads ALL Tdl taps from `live_profile.taps[..n_taps]`
    // instead of the YAML `step.taps`. Set by the snap path on the first
    // accepted profile_swap REQ for this link. `chain_has_leading_tdl` is
    // cached at prepare() so the snap path can do the eligibility check
    // without re-walking the chain each slot.
    ProfileShadow live_profile;
    bool          live_profile_active = false;
    bool          chain_has_leading_tdl = false;
  };

  LinkState& ensure_link_state(const std::string& link_key,
                               const ModelConfig& model,
                               std::size_t sample_count);

  // One-shot setup for a Tdl step's per-link runtime state. Called from
  // ensure_link_state whenever a step in the chain is a Tdl step. Static
  // because it owns no instance state -- the StepState reference carries
  // everything it needs to populate. `fading_seed` is hashed from the link
  // key + step index at the call site so both backends draw the same Jakes
  // sub-ray angles / phases when fading is enabled.
  static void prepare_tdl_step(StepState& state, const ModelStep& step,
                               std::uint64_t fading_seed);

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
