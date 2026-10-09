#pragma once
// Runtime-mutable per-link channel parameters (Phase 3 v1 — scalar params).
//
// MutableParams sits inside each link's per-backend state struct
// (DeviceLinkState::live on CUDA, LinkState::live on CPU). At prepare()
// it is populated from the YAML initial state via populate_mutable_params_from_yaml.
// In subsequent commits the ZMQ control plane writes to a per-link shadow
// of this struct; a snap-at-slot-boundary step copies shadow → live before
// the H2D so the kernel reads consistent values for the whole slot. See
// docs/history/designs/runtime-mutable-channel.md for the full design.
//
// POD layout: cudaMemcpy-safe, no padding surprises. The 32-byte size is
// asserted so future field additions don't silently break the binary copy
// path between host shadow and device live.

#include "ocudu_gpu_channel/config.h"

#include <cstdint>

namespace ocg {

// Runtime control and device storage share this bound.
constexpr int kMaxProfileDelaySamples = 1023;

struct MutableParams {
  // Chain-step params (consumed by the per-sample CPU chain or by the
  // superpose_kernel's apply_chain device function on CUDA).
  //
  // `awgn_snr_db` is the SNR knob the runtime exposes. Both backends
  // derive per-component σ from current input power and this value at
  // execute time: σ = sqrt((Pin / 10^(snr_db/10)) / 2). When a YAML chain
  // uses an explicit `noise_power`, populate_mutable_params_from_yaml
  // converts it back to an equivalent snr_db against a reference power
  // of 1.0 so the runtime control plane has a single knob to write.
  float    path_loss_db        = 0.0F;
  float    awgn_snr_db         = 60.0F;
  float    cfo_hz              = 0.0F;

  // Per-edge channel params (consumed by apply_channel_kernel on CUDA or
  // apply_tdl_step{_fading} on CPU). Only the leading tap is mutable in v1;
  // multi-tap profile swaps are v2. tap0_delay_samples is FLOAT so YAMLs
  // with fractional delays (e.g. TR 38.901 TDL-A τ=2.5 samples) round-trip
  // through the runtime control plane lossless; both backends recompute
  // the per-tap polyphase coefficients from the fractional part on snap.
  float    tap0_delay_samples  = 0.0F;
  float    tap0_gain_db        = 0.0F;
  float    tap0_phase_rad      = 0.0F;
  float    los_k_db            = 0.0F;

  uint32_t _pad                = 0;   // align to 32 bytes for clean POD copy
};

static_assert(sizeof(MutableParams) == 32,
              "MutableParams must be 32 bytes (cudaMemcpy POD contract); "
              "the runtime control plane copies this struct byte-for-byte.");

// Populate MutableParams from a YAML-derived ModelConfig. The seven v1 params
// are gathered from:
//   - chain[].params["path_loss_db" / "cfo_hz"]: first occurrence in the chain
//   - chain[].params["snr_db" / "noise_power"]: first Awgn step → awgn_sigma
//     (computed as the same per-component sigma the runtime backends use)
//   - chain[0].taps[0].{delay_samples, gain_db, phase_rad}: leading tap of the
//     leading tdl step (if present); otherwise 0
//   - chain[0].fading.los_k_db: leading tdl's fading sub-config (if present)
//
// `reference_power` is the running power before the AWGN step is applied; the
// caller passes the same value the backend uses for its own sigma computation
// (typically the input power for the link). 0 yields awgn_sigma = 0 (an
// explicit noise_power param still works).
//
// `sample_rate_hz` is unused in v1 but kept in the signature for symmetry with
// the chain-step builders and for future params that need it.
MutableParams populate_mutable_params_from_yaml(
    const ModelConfig& model,
    double reference_power,
    std::uint64_t sample_rate_hz);

} // namespace ocg
