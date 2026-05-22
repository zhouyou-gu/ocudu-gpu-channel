#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#include <cstddef>
#include <cstdint>
#include <numbers>
#include <random>
#include <vector>

namespace ocg {

// Length of the windowed-sinc fractional-delay filter used by the `tdl` chain
// step. Eight taps with a Hamming window is the standard SDR convention --
// flat to ~0.05 dB across the passband at 23.04 MS/s for our use case, with a
// per-output-sample cost (8 complex multiply-adds) that does not dominate the
// per-tap loop at the tap counts the validator allows (<= 64). Kept as an
// odd-named constant so future Phase 1.x work can lift it without touching
// every call site.
constexpr int kTdlFracFilterTaps = 8;

// Builds an 8-tap Hamming-windowed sinc filter that resolves a fractional
// sample offset `frac` in [0, 1). The filter is convolved against input
// samples around the integer-aligned read position so that
//   y[n] = sum_{i=0..7} out[i] * x[(n - tau_int) + 3 - i]
// yields x evaluated at time (n - tau_int - frac). For frac == 0 the
// coefficients collapse to a unit impulse at i == 3 (sinc(integer) = 0
// elsewhere, sinc(0) = 1), so an integer-only delay falls out of this same
// filter without a separate code path. Coefficients are DC-normalised so a
// constant input passes through unchanged.
inline void compute_windowed_sinc_taps(double frac,
                                       std::array<float, kTdlFracFilterTaps>& out)
{
  constexpr int N = kTdlFracFilterTaps;
  constexpr int K = N / 2;
  constexpr double pi = std::numbers::pi;

  double sum = 0.0;
  for (int i = 0; i < N; ++i) {
    const double x = static_cast<double>(i - (K - 1)) - frac;
    double sinc_x;
    if (std::fabs(x) < 1e-12) {
      sinc_x = 1.0;
    } else {
      const double pix = pi * x;
      sinc_x = std::sin(pix) / pix;
    }
    // Hamming window over the 8-tap aperture, centred on (N - 1) / 2 = 3.5.
    const double window =
        0.54 - 0.46 * std::cos(2.0 * pi * static_cast<double>(i) / static_cast<double>(N - 1));
    const double coeff = sinc_x * window;
    out[static_cast<std::size_t>(i)] = static_cast<float>(coeff);
    sum += coeff;
  }
  if (std::fabs(sum) > 1e-12) {
    const float inv_sum = static_cast<float>(1.0 / sum);
    for (auto& c : out) {
      c *= inv_sum;
    }
  }
}

// One-shot setup for a `tdl` chain step's per-link runtime state, shared by the
// CPU and CUDA backends so the polyphase coefficient set and the cross-slot
// `delay_line` ring are computed identically on either path.
//
// `taps` is a const ref to the step's tap list in the owning `ModelConfig`
// (which persists for the run) -- the helper does not cache or copy the tap
// data. `polyphase` is sized to one 8-tap Hamming-windowed sinc coefficient set
// per tap; `delay_line` is sized to `ceil(max tau_k) + kTdlFracFilterTaps`
// samples so the filter for the largest-tau tap always reads from valid data
// in the previous-slot tail. Both are mutated in place; both must outlive the
// link's processing calls.
inline void prepare_tdl_state(const std::vector<TapSpec>& taps,
                              std::vector<std::array<float, kTdlFracFilterTaps>>& polyphase,
                              std::vector<IqSample>& delay_line)
{
  polyphase.assign(taps.size(), std::array<float, kTdlFracFilterTaps>{});
  double max_tau = 0.0;
  for (std::size_t k = 0; k != taps.size(); ++k) {
    const double tau = taps[k].delay_samples;
    max_tau = std::max(max_tau, tau);
    const double frac = tau - std::floor(tau);
    compute_windowed_sinc_taps(frac, polyphase[k]);
  }
  const std::size_t ring_size =
      static_cast<std::size_t>(std::ceil(max_tau)) +
      static_cast<std::size_t>(kTdlFracFilterTaps);
  if (delay_line.size() < ring_size) {
    delay_line.assign(ring_size, IqSample{0.0F, 0.0F});
  }
}

// Multi-tap windowed-sinc convolution shared by both backends. For each output
// sample n, sum over taps k:
//   y[n] = sum_k a_k * sum_{i=0..7} polyphase[k][i] * x[(n - floor(tau_k)) + 3 - i]
// where a_k = 10^(gain_db_k/20) * exp(j * phase_rad_k). Indices below zero read
// from the `delay_line` ring (previous-slot tail). The ring is rolled forward
// after the convolution so the next call's negative-index reads stay valid.
//
// `taps` and `polyphase` are read-only views into the link's prepared state;
// `delay_line` is the cross-slot history ring, mutated in place. `in` and
// `out` must not alias. Both CPU and CUDA staging paths call this function --
// there is one implementation of the math.
inline void apply_tdl_step(const IqSample* in,
                           IqSample* out,
                           std::size_t count,
                           const std::vector<TapSpec>& taps,
                           const std::vector<std::array<float, kTdlFracFilterTaps>>& polyphase,
                           std::vector<IqSample>& delay_line)
{
  const auto count_pd = static_cast<std::ptrdiff_t>(count);
  const auto ring_size = static_cast<std::ptrdiff_t>(delay_line.size());
  // Window-sinc forward look: the filter centred at i=3 reads up to n + 3 in
  // the worst case (tau_int = 0, last sample of slot). Those samples are
  // future-of-current-slot and not yet available -- treat them as zero, the
  // same way pre-stream history reads from before delay_line is zero. Bug
  // surfaced by remote RTX 5090 / gcc validation (the Mac/clang build was
  // hiding it because OOB reads happened to return zero in practice).
  const auto read = [&](std::ptrdiff_t idx) -> IqSample {
    if (idx >= count_pd) {
      return IqSample{0.0F, 0.0F};
    }
    if (idx >= 0) {
      return in[static_cast<std::size_t>(idx)];
    }
    const std::ptrdiff_t ring_idx = ring_size + idx;
    if (ring_idx < 0) {
      return IqSample{0.0F, 0.0F};
    }
    return delay_line[static_cast<std::size_t>(ring_idx)];
  };
  for (std::size_t n = 0; n != count; ++n) {
    IqSample acc{0.0F, 0.0F};
    for (std::size_t k = 0; k != taps.size(); ++k) {
      const TapSpec& tap = taps[k];
      const std::ptrdiff_t tau_int =
          static_cast<std::ptrdiff_t>(std::floor(tap.delay_samples));
      const auto& h = polyphase[k];
      IqSample x{0.0F, 0.0F};
      for (int i = 0; i < kTdlFracFilterTaps; ++i) {
        const std::ptrdiff_t read_idx =
            static_cast<std::ptrdiff_t>(n) - tau_int + 3 - i;
        const IqSample s = read(read_idx);
        x.i += h[static_cast<std::size_t>(i)] * s.i;
        x.q += h[static_cast<std::size_t>(i)] * s.q;
      }
      const float gain = static_cast<float>(std::pow(10.0, tap.gain_db / 20.0));
      const float cos_phi = static_cast<float>(std::cos(tap.phase_rad));
      const float sin_phi = static_cast<float>(std::sin(tap.phase_rad));
      const float rotated_i = x.i * cos_phi - x.q * sin_phi;
      const float rotated_q = x.i * sin_phi + x.q * cos_phi;
      acc.i += gain * rotated_i;
      acc.q += gain * rotated_q;
    }
    out[n] = acc;
  }
  // Roll the ring forward: the new tail is the last `delay_line.size()` samples
  // of the current slot's input. If the slot is shorter than the ring, shift
  // the existing tail left and append all of the current slot at the end.
  const std::size_t dl_size = delay_line.size();
  if (count >= dl_size) {
    std::copy(in + (count - dl_size), in + count, delay_line.begin());
  } else {
    const std::size_t keep_old = dl_size - count;
    std::move(delay_line.begin() + static_cast<std::ptrdiff_t>(count),
              delay_line.end(), delay_line.begin());
    std::copy(in, in + count,
              delay_line.begin() + static_cast<std::ptrdiff_t>(keep_old));
  }
}

// Number of sinusoids per tap in the Jakes / sum-of-sinusoids generator.
// Zheng-Xiao (2003) recommend M >= 8 for WSS-correct output; Sionna's TDL
// uses 20 as its default. 20 is also the canonical published choice. Kept as
// a constexpr so future Phase 1.x can lift it without touching every site.
constexpr int kTdlFadingSinusoids = 20;

// Per-link runtime state for the `tdl` fading sub-config. Populated once at
// prepare time from a deterministic seed; mutated per slot (only the
// slot_start_samples accumulator advances). All randomness is in the per-tap
// alpha / phi arrays drawn here; the kernel turns them into a Doppler-shaped
// stochastic process at apply time. tap_alpha / tap_phi / tap_phi_los are
// sized to the tap count; non-LOS taps still get a tap_phi_los draw (kept for
// determinism: same seed -> same random sequence regardless of which taps are
// LOS).
struct TdlFadingState {
  bool enabled = false;
  double f_d_max_hz = 0.0;
  FadingSpectrum spectrum = FadingSpectrum::Jakes;
  double grid_us = 100.0;
  std::vector<std::array<float, kTdlFadingSinusoids>> tap_alpha;  // sub-ray angles in [0, 2pi)
  std::vector<std::array<float, kTdlFadingSinusoids>> tap_phi;    // initial phases in [0, 2pi)
  std::vector<float> tap_phi_los;                                 // LOS specular initial phase
  std::uint64_t slot_start_samples = 0;                           // accumulated sample count
};

// One-shot RNG setup for a tdl step's fading sub-config. Both backends call
// this with the same seed (typically hash of "<link_key>:fading:<step_idx>")
// so a fading-enabled link's stochastic process is bit-identical on CPU and
// CUDA. The seed determines all sub-ray angles, all sub-ray initial phases,
// and every tap's LOS specular initial phase, drawn in a deterministic order
// from std::mt19937_64. No-op when `step.fading_enabled` is false.
//
// Sample rate is NOT needed here -- alpha / phi are dimensionless angles, and
// every per-time-domain conversion (omega_m, grid_stride, per-sample LOS step)
// is done at apply time where the sample rate is in scope.
inline void prepare_tdl_fading_state(const ModelStep& step,
                                     std::uint64_t seed,
                                     TdlFadingState& state)
{
  state.enabled = step.fading_enabled;
  state.f_d_max_hz = step.fading_f_d_max_hz;
  state.spectrum = step.fading_spectrum;
  state.grid_us = step.fading_grid_us;
  state.slot_start_samples = 0;
  if (!step.fading_enabled) {
    state.tap_alpha.clear();
    state.tap_phi.clear();
    state.tap_phi_los.clear();
    return;
  }
  const std::size_t n_taps = step.taps.size();
  state.tap_alpha.assign(n_taps, std::array<float, kTdlFadingSinusoids>{});
  state.tap_phi.assign(n_taps, std::array<float, kTdlFadingSinusoids>{});
  state.tap_phi_los.assign(n_taps, 0.0F);
  std::mt19937_64 rng(seed);
  std::uniform_real_distribution<double> uniform_2pi(0.0,
                                                     2.0 * std::numbers::pi);
  for (std::size_t k = 0; k != n_taps; ++k) {
    for (int m = 0; m < kTdlFadingSinusoids; ++m) {
      state.tap_alpha[k][m] = static_cast<float>(uniform_2pi(rng));
      state.tap_phi[k][m] = static_cast<float>(uniform_2pi(rng));
    }
    // Always draw a LOS phase, even for non-LOS taps -- keeps the per-tap RNG
    // stream advance count fixed so flipping is_los doesn't reshuffle the
    // remaining taps' draws.
    state.tap_phi_los[k] = static_cast<float>(uniform_2pi(rng));
  }
}

// Multi-tap convolution with Doppler-spread fading. The non-fading variant
// (apply_tdl_step above) handles the static case; this one is called when the
// link's tdl step has `fading_enabled = true`. The shared math is the same
// 8-tap windowed-sinc polyphase + cross-slot delay_line ring; on top of that,
// each tap k's complex gain becomes a stationary stochastic process g_k(t)
// derived from the Jakes generator:
//
//   g_k(t) = (1 / sqrt(M)) * sum_{m=1..M} exp(j(2*pi * f_d_max * cos(alpha_{k,m}) * t
//                                              + phi_{k,m}))
//
// g_k(t) is computed on a coarse grid (stride = grid_us * sample_rate) with
// phase accumulation so per-sub-ray cost is one complex multiply per grid
// step, not a sin/cos. The per-sample g_k(t_n) is recovered by linear
// interpolation between adjacent grid points. For LOS taps (is_los = true),
// the Rician composition is:
//
//   g_k(t) = sqrt(K/(K+1)) * exp(j(2*pi * f_d_max * cos(los_angle_rad) * t
//                                  + phi_LOS))
//          + sqrt(1/(K+1)) * g_k_rayleigh(t)
//
// where K = 10^(los_k_db/10). The specular phase advances per sample via a
// pre-computed `exp(j*omega_los/sample_rate)` multiplier; no per-sample trig.
//
// Sample-rate-relative time evolves continuously across calls via
// `fading.slot_start_samples`, which is advanced by `count` at the end of
// every call so subsequent slots pick up where the previous one left off.
//
// Spectra other than Jakes are not implemented in Phase 1.4b -- the function
// throws on encountering them. Phase 1.5+ can drop them in behind the same
// API.
inline void apply_tdl_step_fading(const IqSample* in,
                                  IqSample* out,
                                  std::size_t count,
                                  const std::vector<TapSpec>& taps,
                                  const std::vector<std::array<float, kTdlFracFilterTaps>>& polyphase,
                                  std::vector<IqSample>& delay_line,
                                  TdlFadingState& fading,
                                  std::uint64_t sample_rate_hz)
{
  if (!fading.enabled) {
    apply_tdl_step(in, out, count, taps, polyphase, delay_line);
    return;
  }
  if (fading.spectrum != FadingSpectrum::Jakes) {
    throw std::runtime_error(
        "tdl fading spectrum other than 'jakes' is not implemented yet "
        "(Phase 1.4b ships Jakes; gaussian/flat reserved for a later phase)");
  }
  using complex_f = std::complex<float>;
  const std::size_t n_taps = taps.size();
  const double sr = static_cast<double>(sample_rate_hz);
  const double two_pi = 2.0 * std::numbers::pi;
  const double inv_sqrt_M =
      1.0 / std::sqrt(static_cast<double>(kTdlFadingSinusoids));

  // Coarse-grid sizing: stride in samples, count of grid points covering the
  // slot plus a +1 endpoint for the last interpolation interval. Minimum
  // stride 1 sample so a degenerate `grid_us` (or extremely high sample rate)
  // still produces a valid grid.
  const std::size_t grid_stride = std::max<std::size_t>(
      1, static_cast<std::size_t>(std::round(fading.grid_us * 1.0e-6 * sr)));
  const std::size_t grid_count = (count + grid_stride - 1) / grid_stride + 1;

  // Per-tap g_k_rayleigh(t) sampled on the coarse grid. Built via phase
  // accumulation so trig cost is O(M * n_taps) per slot, not O(M * grid_count
  // * n_taps).
  std::vector<std::vector<complex_f>> g_grid(n_taps,
                                             std::vector<complex_f>(grid_count));
  const double slot_start_t =
      static_cast<double>(fading.slot_start_samples) / sr;
  const double grid_dt = static_cast<double>(grid_stride) / sr;
  for (std::size_t k = 0; k != n_taps; ++k) {
    for (int m = 0; m < kTdlFadingSinusoids; ++m) {
      const double omega_m =
          two_pi * fading.f_d_max_hz * std::cos(fading.tap_alpha[k][m]);
      const double angle0 = omega_m * slot_start_t + fading.tap_phi[k][m];
      complex_f current{static_cast<float>(std::cos(angle0)),
                        static_cast<float>(std::sin(angle0))};
      const double step_angle = omega_m * grid_dt;
      const complex_f step_mul{static_cast<float>(std::cos(step_angle)),
                               static_cast<float>(std::sin(step_angle))};
      for (std::size_t g = 0; g != grid_count; ++g) {
        g_grid[k][g] += current;
        current *= step_mul;
      }
    }
    for (auto& v : g_grid[k]) {
      v *= static_cast<float>(inv_sqrt_M);
    }
  }

  // Per-sample LOS specular phase accumulators (only for LOS taps). For
  // non-LOS taps these stay at their default {1, 0} / {0, 0} and are not read.
  std::vector<complex_f> los_current(n_taps, complex_f{0.0F, 0.0F});
  std::vector<complex_f> los_step(n_taps, complex_f{1.0F, 0.0F});
  std::vector<float> los_factor(n_taps, 0.0F);
  std::vector<float> rayleigh_factor(n_taps, 1.0F);
  for (std::size_t k = 0; k != n_taps; ++k) {
    if (!taps[k].is_los) {
      continue;
    }
    const double K_lin = std::pow(10.0, taps[k].los_k_db / 10.0);
    los_factor[k] = static_cast<float>(std::sqrt(K_lin / (K_lin + 1.0)));
    rayleigh_factor[k] = static_cast<float>(std::sqrt(1.0 / (K_lin + 1.0)));
    const double omega_los =
        two_pi * fading.f_d_max_hz * std::cos(taps[k].los_angle_rad);
    const double angle0 = omega_los * slot_start_t + fading.tap_phi_los[k];
    los_current[k] = complex_f{static_cast<float>(std::cos(angle0)),
                               static_cast<float>(std::sin(angle0))};
    const double step_angle = omega_los / sr;
    los_step[k] = complex_f{static_cast<float>(std::cos(step_angle)),
                            static_cast<float>(std::sin(step_angle))};
  }

  const auto count_pd = static_cast<std::ptrdiff_t>(count);
  const auto ring_size = static_cast<std::ptrdiff_t>(delay_line.size());
  const auto read = [&](std::ptrdiff_t idx) -> IqSample {
    if (idx >= count_pd) {
      return IqSample{0.0F, 0.0F};
    }
    if (idx >= 0) {
      return in[static_cast<std::size_t>(idx)];
    }
    const std::ptrdiff_t ring_idx = ring_size + idx;
    if (ring_idx < 0) {
      return IqSample{0.0F, 0.0F};
    }
    return delay_line[static_cast<std::size_t>(ring_idx)];
  };

  const float grid_stride_f = static_cast<float>(grid_stride);
  for (std::size_t n = 0; n != count; ++n) {
    // Linear interpolation index for sample n into the coarse grid.
    const std::size_t g_floor = n / grid_stride;
    const std::size_t g_ceil_idx = std::min(g_floor + 1, grid_count - 1);
    const float frac =
        static_cast<float>(n - g_floor * grid_stride) / grid_stride_f;

    IqSample acc{0.0F, 0.0F};
    for (std::size_t k = 0; k != n_taps; ++k) {
      // g_k(t_n) -- Rayleigh component interpolated from the grid.
      const complex_f g_rayleigh =
          g_grid[k][g_floor] * (1.0F - frac) + g_grid[k][g_ceil_idx] * frac;
      complex_f g_k = g_rayleigh;
      if (taps[k].is_los) {
        g_k = los_factor[k] * los_current[k] +
              rayleigh_factor[k] * g_rayleigh;
      }
      // Polyphase convolution against the tap-modulated input. Read the 8 tap
      // samples around (n - tau_int) then apply static phase + gain, then
      // multiply by the time-varying complex g_k(t_n).
      const TapSpec& tap = taps[k];
      const std::ptrdiff_t tau_int =
          static_cast<std::ptrdiff_t>(std::floor(tap.delay_samples));
      const auto& h = polyphase[k];
      IqSample x{0.0F, 0.0F};
      for (int i = 0; i < kTdlFracFilterTaps; ++i) {
        const std::ptrdiff_t read_idx =
            static_cast<std::ptrdiff_t>(n) - tau_int + 3 - i;
        const IqSample s = read(read_idx);
        x.i += h[static_cast<std::size_t>(i)] * s.i;
        x.q += h[static_cast<std::size_t>(i)] * s.q;
      }
      const float gain = static_cast<float>(std::pow(10.0, tap.gain_db / 20.0));
      const float cos_phi_s = static_cast<float>(std::cos(tap.phase_rad));
      const float sin_phi_s = static_cast<float>(std::sin(tap.phase_rad));
      const float static_i = x.i * cos_phi_s - x.q * sin_phi_s;
      const float static_q = x.i * sin_phi_s + x.q * cos_phi_s;
      const float xi = static_i * g_k.real() - static_q * g_k.imag();
      const float xq = static_i * g_k.imag() + static_q * g_k.real();
      acc.i += gain * xi;
      acc.q += gain * xq;
    }
    out[n] = acc;
    // Advance per-sample LOS phase accumulators after consuming sample n.
    for (std::size_t k = 0; k != n_taps; ++k) {
      if (taps[k].is_los) {
        los_current[k] *= los_step[k];
      }
    }
  }

  // Roll the cross-slot ring forward (same as the static apply_tdl_step). The
  // tap-modulated samples are written to `out`; the ring stores raw input so
  // the next slot's polyphase reads have unmodulated history.
  const std::size_t dl_size = delay_line.size();
  if (count >= dl_size) {
    std::copy(in + (count - dl_size), in + count, delay_line.begin());
  } else {
    const std::size_t keep_old = dl_size - count;
    std::move(delay_line.begin() + static_cast<std::ptrdiff_t>(count),
              delay_line.end(), delay_line.begin());
    std::copy(in, in + count,
              delay_line.begin() + static_cast<std::ptrdiff_t>(keep_old));
  }

  // Advance the per-link slot-start accumulator for the next call's t origin.
  fading.slot_start_samples += count;
}

} // namespace ocg
