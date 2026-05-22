#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <numbers>
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

// Applies an integer/fractional sample delay to `count` input samples, writing
// `count` delayed samples to `out`. Output sample n is the linear interpolation
// between input samples (n - integer_delay) and (n - integer_delay - 1), with a
// `fraction` in [0, 1) selecting between them; an integer delay passes
// fraction = 0.
//
// `delay_line` carries the previous batch's tail so the delay stays continuous
// across calls -- pass the same vector every call for one link (it starts empty
// and is grown and maintained internally; the implied pre-stream history is
// zero). `out` must not alias `in`.
//
// This is the single source of truth for the delay math: the CPU backend and
// the CUDA backend both call it, so a delayed link is bit-identical on either.
inline void apply_sample_delay(const IqSample* in,
                               IqSample* out,
                               std::size_t count,
                               std::size_t integer_delay,
                               double fraction,
                               std::vector<IqSample>& delay_line)
{
  // History must reach back to index (n - integer_delay - 1) for n = 0.
  const std::size_t history_size = integer_delay + 2;
  if (delay_line.size() < history_size) {
    delay_line.insert(delay_line.begin(), history_size - delay_line.size(), IqSample{});
  }

  // Negative indices read the previous batch's tail held in delay_line.
  auto sample_at = [&](std::ptrdiff_t index) -> IqSample {
    if (index >= 0) {
      return in[static_cast<std::size_t>(index)];
    }
    return delay_line[static_cast<std::size_t>(static_cast<std::ptrdiff_t>(delay_line.size()) + index)];
  };

  for (std::size_t n = 0; n != count; ++n) {
    const auto delayed_index = static_cast<std::ptrdiff_t>(n) - static_cast<std::ptrdiff_t>(integer_delay);
    const IqSample sample0 = sample_at(delayed_index);
    const IqSample sample1 = sample_at(delayed_index - 1);
    out[n].i = static_cast<float>((1.0 - fraction) * sample0.i + fraction * sample1.i);
    out[n].q = static_cast<float>((1.0 - fraction) * sample0.q + fraction * sample1.q);
  }

  // Retain the last history_size samples of the input stream for the next call.
  if (count >= history_size) {
    std::copy(in + (count - history_size), in + count, delay_line.begin());
  } else {
    const std::size_t keep_old = history_size - count;
    std::move(delay_line.end() - static_cast<std::ptrdiff_t>(keep_old), delay_line.end(), delay_line.begin());
    std::copy(in, in + count, delay_line.begin() + static_cast<std::ptrdiff_t>(keep_old));
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

} // namespace ocg
