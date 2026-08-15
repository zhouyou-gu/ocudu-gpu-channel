#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/iq.h"
#include "ocudu_gpu_channel/physical_link.h"
#include "ocudu_gpu_channel/mutable_params.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/runtime_control.h"
#include <array>
#include <complex>
#include <set>
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

  // Keep the base class's single-row convenience overload visible; overriding
  // the row-vector virtual below would otherwise hide it.
  using ChannelProcessor::process_superposition;

  void process_superposition(const std::string& dst_key,
                             const std::vector<SuperpositionInput>& inputs,
                             const ModelConfig* rx_model,
                             std::uint64_t sample_rate_hz,
                             std::span<std::span<IqSample>> outputs) override;

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

  // All state owned by one LANE: two ping-pong scratch buffers, one StepState
  // per model-chain step, and its position in the link's matrix. A lane is
  // processed by a single thread, so it needs no internal locking.
  //
  // What is NOT here, since M4.2: the runtime-control block, `live`, the slot
  // counter and the profile state. Those belong to the physical link, because a
  // lane that could hold its own copy could take a swap in a different slot
  // from its siblings.
  struct LinkState {
    IqBuffer scratch_a;
    IqBuffer scratch_b;
    std::vector<StepState> steps;

    // The lane's materialised view of the runtime-mutable parameters.
    //
    // The DECISION to change them is the link's (one shadow, one seqno, one
    // slot gate); the VALUES are per lane, and the snap writes the link's
    // snapped view into every lane of the link in the same slot. Keeping the
    // values per lane is what preserves a fixed_mimo matrix: its per-lane tap
    // weights are folded into per-lane model clones at load time, so a single
    // shared `live` would overwrite them all with one lane's numbers before any
    // control plane was even wired. An actual control update DOES flatten them
    // -- which is why a tap-scope update is rejected on a fixed_mimo model.
    MutableParams live;

    // The link this lane belongs to. Borrowed from `links_`, which outlives
    // every LinkState; all lanes of a link share one, and none of them advances
    // its clock or snaps its control -- process_superposition does, once per
    // slot per link.
    PhysicalLinkRuntime* link = nullptr;
    int lane_index = 0;
    int lane_count = 1;
    // M3.5: this lane's entry in the link's LOS matrix. 1 + 0j when the model
    // declares none, which is the all-ones rank-1 LOS -- one specular path
    // seen with the same phase by every antenna pair.
    std::complex<float> los_coefficient{1.0F, 0.0F};
  };

  // `physical_link_key` / `rx_port` / `tx_port` name the lane's position in
  // its physical link. They are only read when the state is created, and only
  // to derive the stochastic channel's seed (M2): a lane's realisation follows
  // from the link's identity and the lane's matrix position, never from the
  // spelling of `link_key`.
  LinkState& ensure_link_state(const std::string& link_key,
                               const ModelConfig& model,
                               std::size_t sample_count,
                               const LaneIdentity& identity);

  // Builds this slot's fading grids for one lane, into the row its physical
  // link owns. Runs for every lane of the slot BEFORE any lane is shaped, so
  // the cross-lane step M3.4 adds has every row to work with.
  static void refresh_lane_grids(LinkState& state, const ModelConfig& model,
                                 std::size_t count, std::uint64_t sample_rate_hz);

  // One-shot setup for a Tdl step's per-lane runtime state. Static because it
  // owns no instance state -- the StepState reference carries everything it
  // needs. `fading_seed` comes from physical_link_seed + lane_fading_seed at
  // the call site, so both backends draw the same Jakes sub-rays for a lane.
  static void prepare_tdl_step(StepState& state, const ModelStep& step,
                               std::uint64_t fading_seed);

  // Internal helper used by process_superposition() to shape one edge's
  // input through its model chain into the provided output span. This is
  // what process_into() used to be -- now private since the public API only
  // exposes the per-node superposition entry point.
  // Returns the physical link the shaped lane read its time and parameters
  // from, so the caller can advance its clock once the slot is complete.
  PhysicalLinkRuntime* apply_chain_to_link(const std::string& link_key,
                                         const ModelConfig& model,
                                         std::span<const IqSample> input,
                                         std::span<IqSample> output,
                                         std::uint64_t sample_rate_hz);

  std::unordered_map<std::string, LinkState> states_;
  // Keyed by physical link identity (LaneConfig::physical_link_key). Node-based
  // storage, so a LinkState may hold a pointer into it across insertions.
  std::unordered_map<std::string, PhysicalLinkRuntime> links_;
  // Which state keys are receiver-model rows rather than lanes. They are not
  // links, so M4.2 keeps them off the control surface (the CUDA backend never
  // exposed them, and the two disagreeing was a REQ succeeding on one backend
  // and failing on the other).
  std::set<std::string> rx_model_keys_;
};

} // namespace ocg
