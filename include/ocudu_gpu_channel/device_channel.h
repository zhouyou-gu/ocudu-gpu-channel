#pragma once
// Device-side per-edge channel pipeline scaffold (Phase 2 D1).
//
// This header declares the data structures and entry points for moving the
// per-edge channel work (multi-tap convolution + Jakes fading + Rician LOS)
// from host-side `stage_link()` onto the GPU. See
// `docs/plans/device-channel-pipeline.md` for the full plan.
//
// D1 status: header + struct definitions only; no kernel implementation, no
// behavioural change. The CUDA backend allocates and populates
// DeviceLinkState[] in prepare() but still uses the host-side stage_link()
// path at serve time. D2 will add the static (non-fading) kernel and a
// dispatch gate.
//
// Memory model:
//   - DeviceLinkState lives in GPU global memory, one per (dst_node × incoming
//     edge). Topology fields are populated once at startup and never change
//     during a run.
//   - The cross-slot state (delay_line, slot_start_samples) is updated by the
//     kernel each serve.
//   - The kernel mirrors per-link state into shared memory for the duration
//     of one slot's worth of work, then writes the cross-slot updates back.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/iq.h"
#include <cstdint>

namespace ocg {

// Maximum taps per link the device-side state can hold. TR 38.901 TDL
// profiles max at 24 taps; 32 leaves headroom for LOS-split / custom
// configurations. Sized as a compile-time constant so DeviceLinkState fits
// in a flat array (no device-side allocation).
constexpr int kDeviceMaxTaps = 32;

// Maximum delay-line ring length. ceil(max_tap_delay_samples) + the 8-tap
// polyphase filter span + slack. 128 covers TDL-A at 100 ns DS / 23.04 MS/s
// (max scaled delay ~22 samples) and the longer TDL-E configurations.
constexpr int kDeviceMaxDelayLine = 128;

// Reuse the host-side sub-ray count so device and host agree on the Jakes
// generator.
constexpr int kDeviceMaxFadingSubrays = kTdlFadingSinusoids;

// One per (dst_node × incoming edge). Built host-side from
// LinkModelState + TdlFadingState + the link's TapSpec array, copied to
// device once at prepare(). The kernel reads the topology fields read-only
// and updates only the cross-slot state (delay_line, slot_start_samples).
//
// All fields are POD so the struct is cudaMemcpy-able.
struct DeviceLinkState {
  // ---- Topology (read-only after prepare) -------------------------------
  int n_taps;                                                      // 1..kDeviceMaxTaps
  int has_tdl;                                                     // 0 if the link has no leading tdl
  int delay_line_size;                                             // active prefix of delay_line[]
  int reserved0;                                                   // pad to keep alignment tidy

  int   tap_delay_int[kDeviceMaxTaps];                             // floor(delay_samples)
  float tap_frac[kDeviceMaxTaps];                                  // delay_samples - floor
  float tap_gain_amp[kDeviceMaxTaps];                              // 10^(gain_db/20)
  float tap_cos_phi[kDeviceMaxTaps];                               // cos(phase_rad)
  float tap_sin_phi[kDeviceMaxTaps];                               // sin(phase_rad)
  float tap_polyphase[kDeviceMaxTaps][kTdlFracFilterTaps];         // 8-tap windowed-sinc

  // ---- Fading config (read-only after prepare) --------------------------
  int   fading_enabled;                                            // 0/1
  int   fading_spectrum;                                           // 0=Jakes (only Jakes implemented)
  float f_d_max_hz;
  float grid_us;

  int   tap_is_los[kDeviceMaxTaps];                                // 0/1 per tap
  float tap_los_factor[kDeviceMaxTaps];                            // sqrt(K/(K+1))
  float tap_rayleigh_factor[kDeviceMaxTaps];                       // sqrt(1/(K+1))
  float tap_omega_los[kDeviceMaxTaps];                             // 2*pi*f_d_max*cos(los_angle_rad)
  float tap_phi_los[kDeviceMaxTaps];                               // LOS initial phase

  float tap_alpha[kDeviceMaxTaps][kDeviceMaxFadingSubrays];        // sub-ray angles (uniform [0, 2pi))
  float tap_phi[kDeviceMaxTaps][kDeviceMaxFadingSubrays];          // sub-ray initial phases

  // ---- Cross-slot state (kernel reads + writes each serve) --------------
  IqSample delay_line[kDeviceMaxDelayLine];
  unsigned long long slot_start_samples;
};

// Populate a DeviceLinkState from the matching host-side ModelStep + the
// per-link fading state (alpha/phi/phi_los) that was already drawn host-side
// in prepare_tdl_fading_state(). Polyphase coefficients are taken from the
// host-side cache rather than recomputed. This runs at prepare() time only,
// once per link.
//
// `host_polyphase` and the alpha/phi arrays must already be sized for the
// step's tap count (the caller's responsibility — typically taken straight
// from LinkModelState::tdl_polyphase and TdlFadingState::tap_alpha/phi).
//
// Returns false if the link has more taps than kDeviceMaxTaps or a tap delay
// exceeds the ring capacity; in that case the caller should fall back to the
// host-side stage_link() path for this link.
bool build_device_link_state(
    const ModelStep& step,
    const std::vector<std::array<float, kTdlFracFilterTaps>>& host_polyphase,
    const TdlFadingState& host_fading,
    int delay_line_size,
    DeviceLinkState& out);

// Phase 2 D2: static (non-fading) per-edge channel kernel.
//
// For each (edge k, output sample idx), computes the multi-tap polyphase
// convolution
//     y[k][idx] = sum_{kt=0..n_taps-1} gain_kt · e^{j·phi_kt}
//                     · sum_{i=0..7} h_kt[i] · x[k][idx − tau_int_kt + 3 − i]
// where `h_kt` is the 8-tap windowed-sinc polyphase coefficient set
// precomputed host-side, and x reads come from `in_buffer` for indices in
// [0, count) or from `states[k].delay_line` for negative indices. Future
// indices (idx − tau_int + 3 − i >= count) read as zero, matching the host
// kernel.
//
// `in_buffer`  : `n_links * count` IqSamples (raw, per-edge slots)
// `out_buffer` : `n_links * count` IqSamples (shaped, per-edge slots)
// `states`     : `n_links` DeviceLinkStates (topology, read-only)
//
// Non-tdl links (`states[k].has_tdl == 0`) pass-through unchanged.
//
// Launch: dim3(ceil(count / 256), n_links), dim3(256, 1, 1).
//
// D2 status: kernel compiled but NOT dispatched from the broker hot-path
// yet. process_superposition() still uses host-side stage_link(). A future
// D2b commit wires the dispatch behind a per-link gate.
// `stream` is a `cudaStream_t` passed as `void*` so this header stays
// includable without a CUDA toolchain. The implementation casts back.
void launch_apply_channel_kernel_static(
    const DeviceLinkState* states,
    const IqSample* in_buffer,
    IqSample* out_buffer,
    int n_links,
    int count,
    void* stream);

// Phase 2 D2: roll the per-link delay_line ring forward and advance
// slot_start_samples by `count`. Called after launch_apply_channel_kernel
// so the cross-slot continuity matches the host-side apply_tdl_step's
// post-loop ring update in delay.h.
//
// Launch: dim3(n_links), dim3(1) — serial per link. The work per link is
// at most kDeviceMaxDelayLine memory ops, trivial.
void launch_update_delay_line_kernel(
    DeviceLinkState* states,
    const IqSample* in_buffer,
    int n_links,
    int count,
    void* stream);

} // namespace ocg
