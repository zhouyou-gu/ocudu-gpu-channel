// Device-side per-edge channel pipeline (Phase 2, shipped through D3).
//
// build_device_link_state() runs once per link in prepare(), populating a
// per-(dst_node x edge) DeviceLinkState array. apply_channel_kernel +
// update_delay_line_kernel are dispatched per serve from
// CudaChannelProcessor::process_superposition() when every incoming edge
// of a destination node has a leading tdl step. Mixed nodes fall back to
// host-side stage_link.
//
// Plan and rationale: docs/plans/device-channel-pipeline.md.

#include "ocudu_gpu_channel/device_channel.h"
#include <cmath>
#include <cstring>
#include <cuda_runtime.h>

namespace ocg {
namespace {

// Per-sample, per-edge multi-tap polyphase convolution + optional
// time-varying Jakes fading + Rician LOS specular. Mirrors apply_tdl_step
// / apply_tdl_step_fading in delay.h: each output sample sums K taps'
// (gain · static-phase-rotation · g_k(t) · windowed-sinc-delayed input).
//
// When s->fading_enabled == 0 the kernel reduces to the static case
// (g_k(t) ≡ 1 + 0j) and the math collapses to the convolution path.
// When s->fading_enabled == 1 the block cooperatively materialises a
// per-tap coarse-grid g_k(t) in shared memory at block start, then
// each thread interpolates g_k(t_idx) for its sample and composes the
// Rician LOS specular per-tap if is_los.
//
// Launch: grid = dim3(ceil(count / 256), n_links), block = dim3(256).
//   blockIdx.x → sample block within slot
//   blockIdx.y → edge index k
//
// Shared memory: ~8.4 KB per block (g_grid_i + g_grid_q +
// los_phase_start + los_step_angle). Fits well within modern SM budgets.
__global__ void apply_channel_kernel(
    const DeviceLinkState* __restrict__ states,
    const IqSample* __restrict__ in_buffer,
    IqSample* __restrict__ out_buffer,
    int n_links,
    int count,
    float sample_rate_hz)
{
  // Per-block shared state for the fading path. Sized at compile time so
  // no extern shared memory plumbing at the call site.
  __shared__ float g_grid_i[kDeviceMaxTaps][kDeviceMaxGridPoints];
  __shared__ float g_grid_q[kDeviceMaxTaps][kDeviceMaxGridPoints];
  __shared__ float los_phase_start[kDeviceMaxTaps];
  __shared__ float los_step_angle[kDeviceMaxTaps];
  __shared__ int   shared_grid_stride;
  __shared__ int   shared_grid_count;

  const int k = blockIdx.y;
  if (k >= n_links) {
    return;
  }
  const DeviceLinkState* s = &states[k];

  // Non-tdl link: pass-through (the kernel still runs on these for uniformity;
  // the cost is one global-memory read + write per sample, negligible).
  if (s->has_tdl == 0) {
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < count) {
      out_buffer[k * count + idx] = in_buffer[k * count + idx];
    }
    return;
  }

  const int n_taps = s->n_taps;
  const int dl_size = s->delay_line_size;
  const bool fading_on = (s->fading_enabled != 0);

  // -------- Cooperatively materialise the per-tap Jakes coarse grid and
  // per-tap LOS phase constants in shared memory. Only when fading is on;
  // when off, g_k(t) is identically (1, 0) and these arrays go unread.
  if (fading_on) {
    // Compute grid sizing once per block.
    if (threadIdx.x == 0) {
      int stride = (int)roundf(s->grid_us * 1.0e-6f * sample_rate_hz);
      if (stride < 1) stride = 1;
      int gcount = (count + stride - 1) / stride + 1;
      if (gcount > kDeviceMaxGridPoints) gcount = kDeviceMaxGridPoints;
      shared_grid_stride = stride;
      shared_grid_count = gcount;
    }
    __syncthreads();
    const int grid_stride = shared_grid_stride;
    const int grid_count = shared_grid_count;

    // Zero the active part of the grid cooperatively.
    const int grid_total = n_taps * grid_count;
    for (int gi = threadIdx.x; gi < grid_total; gi += blockDim.x) {
      const int kt = gi / grid_count;
      const int gp = gi - kt * grid_count;
      g_grid_i[kt][gp] = 0.0f;
      g_grid_q[kt][gp] = 0.0f;
    }
    __syncthreads();

    const float inv_sqrt_M = rsqrtf((float)kDeviceMaxFadingSubrays);
    // slot_start_t stays in double for the angle0 product; the float version
    // is only used for step_angle (small, no drift across grid points).
    const double slot_start_t_d =
        (double)s->slot_start_samples / (double)sample_rate_hz;
    const float grid_dt = (float)grid_stride / sample_rate_hz;
    constexpr double kTwoPiD = 6.28318530717958647692;

    // One thread per tap accumulates that tap's M sub-rays across the grid
    // via phase accumulation (so each grid point costs one complex multiply
    // per sub-ray, not a sincos). n_taps <= kDeviceMaxTaps = 32 < blockDim.x.
    if (threadIdx.x < n_taps) {
      const int kt = threadIdx.x;
      for (int m = 0; m < kDeviceMaxFadingSubrays; ++m) {
        const float alpha_km = s->tap_alpha[kt][m];
        const float phi_km = s->tap_phi[kt][m];
        // omega_m in double mirrors the host computation in delay.h. Without
        // this the long-run angle0 = omega_m * slot_start_t would lose float
        // precision after minutes of runtime and diverge from the host path.
        const double omega_m_d =
            kTwoPiD * (double)s->f_d_max_hz * cos((double)alpha_km);
        const double angle0_d = omega_m_d * slot_start_t_d + (double)phi_km;
        // Reduce mod 2*pi in double before casting -- matches host fmod path.
        const float angle0 = (float)fmod(angle0_d, kTwoPiD);
        const float omega_m = (float)omega_m_d;
        const float step_angle = omega_m * grid_dt;
        float c_cur, s_cur;
        __sincosf(angle0, &s_cur, &c_cur);
        float c_step, s_step;
        __sincosf(step_angle, &s_step, &c_step);
        for (int gp = 0; gp < grid_count; ++gp) {
          g_grid_i[kt][gp] += c_cur;
          g_grid_q[kt][gp] += s_cur;
          // Multiply (c_cur, s_cur) by (c_step, s_step) for next grid point.
          const float c_new = c_cur * c_step - s_cur * s_step;
          const float s_new = c_cur * s_step + s_cur * c_step;
          c_cur = c_new;
          s_cur = s_new;
        }
      }
      // Scale by 1/sqrt(M).
      for (int gp = 0; gp < grid_count; ++gp) {
        g_grid_i[kt][gp] *= inv_sqrt_M;
        g_grid_q[kt][gp] *= inv_sqrt_M;
      }
      // LOS phase constants for this tap. omega_los recomputed in double so
      // the angle0 product against slot_start_t doesn't drift in float, then
      // reduced mod 2*pi -- same treatment as the subray loop above.
      if (s->tap_is_los[kt]) {
        const double omega_los_d =
            kTwoPiD * (double)s->f_d_max_hz *
            cos((double)s->tap_los_angle_rad[kt]);
        const double angle0_d =
            omega_los_d * slot_start_t_d + (double)s->tap_phi_los[kt];
        los_phase_start[kt] = (float)fmod(angle0_d, kTwoPiD);
        los_step_angle[kt] = (float)(omega_los_d / (double)sample_rate_hz);
      }
    }
    __syncthreads();
  }

  // -------- Per-sample work.
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= count) {
    return;
  }

  // Interpolation index for this sample into the coarse grid.
  int g_floor = 0, g_ceil = 0;
  float frac = 0.0f;
  if (fading_on) {
    const int gs = shared_grid_stride;
    g_floor = idx / gs;
    g_ceil = g_floor + 1;
    if (g_ceil >= shared_grid_count) g_ceil = shared_grid_count - 1;
    if (g_floor >= shared_grid_count) g_floor = shared_grid_count - 1;
    frac = (float)(idx - g_floor * gs) / (float)gs;
  }

  float acc_i = 0.0f;
  float acc_q = 0.0f;

  for (int kt = 0; kt < n_taps; ++kt) {
    // Compute g_k(t_idx). Default is the static identity (1, 0) so the
    // non-fading path collapses to the previous static kernel.
    float g_i = 1.0f;
    float g_q = 0.0f;
    if (fading_on) {
      const float g_ri = g_grid_i[kt][g_floor] * (1.0f - frac) +
                         g_grid_i[kt][g_ceil] * frac;
      const float g_rq = g_grid_q[kt][g_floor] * (1.0f - frac) +
                         g_grid_q[kt][g_ceil] * frac;
      if (s->tap_is_los[kt]) {
        const float los_angle =
            los_phase_start[kt] + los_step_angle[kt] * (float)idx;
        float los_s, los_c;
        __sincosf(los_angle, &los_s, &los_c);
        const float lf = s->tap_los_factor[kt];
        const float rf = s->tap_rayleigh_factor[kt];
        g_i = lf * los_c + rf * g_ri;
        g_q = lf * los_s + rf * g_rq;
      } else {
        g_i = g_ri;
        g_q = g_rq;
      }
    }

    // 8-tap polyphase windowed-sinc read centered around (idx - tau_int).
    const int tau_int = s->tap_delay_int[kt];
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

    // Static phase rotation + g_k(t) modulation + per-tap gain.
    const float cph = s->tap_cos_phi[kt];
    const float sph = s->tap_sin_phi[kt];
    const float rot_i = x_i * cph - x_q * sph;
    const float rot_q = x_i * sph + x_q * cph;
    const float xi = rot_i * g_i - rot_q * g_q;
    const float xq = rot_i * g_q + rot_q * g_i;
    const float gain = s->tap_gain_amp[kt];
    acc_i += gain * xi;
    acc_q += gain * xq;
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
      // Store the angle, not the precomputed omega -- the kernel recomputes
      // omega_los in double for parity-preserving long-run phase precision.
      out.tap_los_angle_rad[k] = static_cast<float>(step.taps[k].los_angle_rad);
    } else {
      out.tap_los_factor[k] = 0.0F;
      out.tap_rayleigh_factor[k] = 1.0F;
      out.tap_los_angle_rad[k] = 0.0F;
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
    float sample_rate_hz,
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
  apply_channel_kernel<<<grid, block, 0, s>>>(states, in_buffer, out_buffer, n_links, count, sample_rate_hz);
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
