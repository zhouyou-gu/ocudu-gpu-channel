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
#include <cuda_runtime.h>

namespace ocg {
namespace {

constexpr double kPiDev = 3.14159265358979323846;

// Per-sample, per-edge multi-tap polyphase convolution (static channel; no
// fading). Mirrors apply_tdl_step() in delay.h: each output sample sums K
// taps' (gain · phase-rotation · windowed-sinc-delayed input), reading
// past-batch history from the per-link delay_line ring.
//
// Launch: grid = dim3(ceil(count / 256), n_links), block = dim3(256).
//   blockIdx.x → sample block within slot
//   blockIdx.y → edge index k
__global__ void apply_channel_kernel(
    const DeviceLinkState* __restrict__ states,
    const IqSample* __restrict__ in_buffer,
    IqSample* __restrict__ out_buffer,
    int n_links,
    int count)
{
  const int k = blockIdx.y;
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (k >= n_links || idx >= count) {
    return;
  }
  const DeviceLinkState* s = &states[k];

  // Non-tdl link: pass-through (the kernel still runs on these for uniformity;
  // the cost is one global-memory read + write per sample, negligible).
  if (s->has_tdl == 0) {
    out_buffer[k * count + idx] = in_buffer[k * count + idx];
    return;
  }

  const int n_taps = s->n_taps;
  const int dl_size = s->delay_line_size;

  float acc_i = 0.0f;
  float acc_q = 0.0f;

  for (int kt = 0; kt < n_taps; ++kt) {
    const int tau_int = s->tap_delay_int[kt];

    // 8-tap polyphase windowed-sinc read centered around (idx - tau_int).
    float x_i = 0.0f;
    float x_q = 0.0f;
    #pragma unroll
    for (int i = 0; i < kTdlFracFilterTaps; ++i) {
      const int read_idx = idx - tau_int + 3 - i;
      float si = 0.0f;
      float sq = 0.0f;
      if (read_idx >= count) {
        // Future read: zero (matches delay.h's apply_tdl_step bound).
      } else if (read_idx >= 0) {
        const IqSample sam = in_buffer[k * count + read_idx];
        si = sam.i;
        sq = sam.q;
      } else {
        // Negative index: read from the per-link delay_line ring tail.
        const int ring_idx = dl_size + read_idx;
        if (ring_idx >= 0 && ring_idx < dl_size) {
          const IqSample sam = s->delay_line[ring_idx];
          si = sam.i;
          sq = sam.q;
        }
        // ring_idx < 0 (pre-history): zero (matches host kernel)
      }
      const float h = s->tap_polyphase[kt][i];
      x_i += h * si;
      x_q += h * sq;
    }

    // Static phase rotation + per-tap gain.
    const float c = s->tap_cos_phi[kt];
    const float sn = s->tap_sin_phi[kt];
    const float rot_i = x_i * c - x_q * sn;
    const float rot_q = x_i * sn + x_q * c;
    const float gain = s->tap_gain_amp[kt];
    acc_i += gain * rot_i;
    acc_q += gain * rot_q;
  }

  IqSample out;
  out.i = acc_i;
  out.q = acc_q;
  out_buffer[k * count + idx] = out;
}

// Roll the per-link delay_line ring forward and advance slot_start_samples.
// Mirrors the post-loop ring update at the bottom of apply_tdl_step() in
// delay.h. One block per link; one thread per block (work per link is at
// most kDeviceMaxDelayLine memory ops, trivially serial).
__global__ void update_delay_line_kernel(
    DeviceLinkState* __restrict__ states,
    const IqSample* __restrict__ in_buffer,
    int n_links,
    int count)
{
  const int k = blockIdx.x;
  if (k >= n_links) {
    return;
  }
  DeviceLinkState* s = &states[k];
  if (s->has_tdl == 0) {
    return;
  }
  const int dl_size = s->delay_line_size;
  if (dl_size <= 0) {
    return;
  }

  if (count >= dl_size) {
    // Last dl_size samples of input become the new ring.
    for (int i = 0; i < dl_size; ++i) {
      s->delay_line[i] = in_buffer[k * count + (count - dl_size) + i];
    }
  } else {
    // Shift ring left by `count`, append all of input at the end.
    const int keep_old = dl_size - count;
    for (int i = 0; i < keep_old; ++i) {
      s->delay_line[i] = s->delay_line[i + count];
    }
    for (int i = 0; i < count; ++i) {
      s->delay_line[keep_old + i] = in_buffer[k * count + i];
    }
  }
  s->slot_start_samples += static_cast<unsigned long long>(count);
}

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

void launch_apply_channel_kernel_static(
    const DeviceLinkState* states,
    const IqSample* in_buffer,
    IqSample* out_buffer,
    int n_links,
    int count,
    void* stream)
{
  if (n_links <= 0 || count <= 0) {
    return;
  }
  constexpr int kBlockThreads = 256;
  const int blocks_x = (count + kBlockThreads - 1) / kBlockThreads;
  const dim3 grid(blocks_x, n_links, 1);
  const dim3 block(kBlockThreads, 1, 1);
  cudaStream_t s = static_cast<cudaStream_t>(stream);
  apply_channel_kernel<<<grid, block, 0, s>>>(states, in_buffer, out_buffer, n_links, count);
}

void launch_update_delay_line_kernel(
    DeviceLinkState* states,
    const IqSample* in_buffer,
    int n_links,
    int count,
    void* stream)
{
  if (n_links <= 0) {
    return;
  }
  cudaStream_t s = static_cast<cudaStream_t>(stream);
  update_delay_line_kernel<<<n_links, 1, 0, s>>>(states, in_buffer, n_links, count);
}

} // namespace ocg
