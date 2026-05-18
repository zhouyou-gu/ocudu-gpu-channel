#pragma once

#include "ocudu_gpu_channel/iq.h"
#include <algorithm>
#include <cstddef>
#include <vector>

namespace ocg {

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
