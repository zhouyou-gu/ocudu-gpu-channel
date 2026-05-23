// Device-side per-edge channel pipeline (Phase 2 D1 scaffold).
//
// D1 status: host-side builder for DeviceLinkState only. No kernel yet.
// The CUDA backend calls build_device_link_state() per link during prepare()
// to populate a per-(dst_node × edge) DeviceLinkState array that will
// eventually be consumed by apply_channel_kernel (added in D2). For now the
// array is allocated and populated but not used at serve time — the
// existing host-side stage_link() path still drives every serve.
//
// Plan and rationale: docs/plans/device-channel-pipeline.md.

#include "ocudu_gpu_channel/device_channel.h"
#include <cmath>
#include <cstring>

namespace ocg {
namespace {

constexpr double kPiDev = 3.14159265358979323846;

} // namespace

bool build_device_link_state(
    const ModelStep& step,
    const std::vector<std::array<float, kTdlFracFilterTaps>>& host_polyphase,
    const TdlFadingState& host_fading,
    int delay_line_size,
    DeviceLinkState& out)
{
  std::memset(&out, 0, sizeof(out));

  // Non-tdl links: caller falls back to the host path. We still zero-init the
  // state so a cudaMemcpy of the array doesn't carry uninitialised bytes.
  if (step.type != ModelStepType::Tdl) {
    out.has_tdl = 0;
    return true;
  }

  const int n_taps = static_cast<int>(step.taps.size());
  if (n_taps < 1 || n_taps > kDeviceMaxTaps) {
    return false;
  }
  if (delay_line_size < 0 || delay_line_size > kDeviceMaxDelayLine) {
    return false;
  }

  out.has_tdl = 1;
  out.n_taps = n_taps;
  out.delay_line_size = delay_line_size;

  for (int k = 0; k < n_taps; ++k) {
    const TapSpec& tap = step.taps[k];
    const double tau_int_d = std::floor(tap.delay_samples);
    out.tap_delay_int[k] = static_cast<int>(tau_int_d);
    out.tap_frac[k] = static_cast<float>(tap.delay_samples - tau_int_d);
    out.tap_gain_amp[k] = static_cast<float>(std::pow(10.0, tap.gain_db / 20.0));
    out.tap_cos_phi[k] = static_cast<float>(std::cos(tap.phase_rad));
    out.tap_sin_phi[k] = static_cast<float>(std::sin(tap.phase_rad));
    out.tap_is_los[k] = tap.is_los ? 1 : 0;

    // Polyphase coefficients from the host-side cache. The host has already
    // computed these via compute_windowed_sinc_taps() in delay.h.
    if (k < static_cast<int>(host_polyphase.size())) {
      for (int i = 0; i < kTdlFracFilterTaps; ++i) {
        out.tap_polyphase[k][i] = host_polyphase[k][i];
      }
    }
  }

  // Fading config
  out.fading_enabled = step.fading_enabled ? 1 : 0;
  out.fading_spectrum = static_cast<int>(step.fading_spectrum);
  out.f_d_max_hz = static_cast<float>(step.fading_f_d_max_hz);
  out.grid_us = static_cast<float>(step.fading_grid_us);

  // Per-tap LOS scalars + sub-ray draws. host_fading was already populated by
  // prepare_tdl_fading_state(); we just copy from it.
  for (int k = 0; k < n_taps; ++k) {
    if (out.tap_is_los[k]) {
      const double K_lin = std::pow(10.0, step.taps[k].los_k_db / 10.0);
      out.tap_los_factor[k] = static_cast<float>(std::sqrt(K_lin / (K_lin + 1.0)));
      out.tap_rayleigh_factor[k] = static_cast<float>(std::sqrt(1.0 / (K_lin + 1.0)));
      out.tap_omega_los[k] = static_cast<float>(
          2.0 * kPiDev * step.fading_f_d_max_hz *
          std::cos(step.taps[k].los_angle_rad));
    } else {
      out.tap_los_factor[k] = 0.0F;
      out.tap_rayleigh_factor[k] = 1.0F;
      out.tap_omega_los[k] = 0.0F;
    }

    if (step.fading_enabled && k < static_cast<int>(host_fading.tap_alpha.size())) {
      for (int m = 0; m < kDeviceMaxFadingSubrays; ++m) {
        out.tap_alpha[k][m] = host_fading.tap_alpha[k][m];
        out.tap_phi[k][m] = host_fading.tap_phi[k][m];
      }
      if (k < static_cast<int>(host_fading.tap_phi_los.size())) {
        out.tap_phi_los[k] = host_fading.tap_phi_los[k];
      }
    }
  }

  out.slot_start_samples = 0ULL;
  return true;
}

} // namespace ocg
