#pragma once

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

} // namespace ocg
