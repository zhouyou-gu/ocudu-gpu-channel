#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include "ocudu_gpu_channel/mutable_params.h"
#include "ocudu_gpu_channel/physical_link.h"
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <span>

namespace ocg {

// Number of evenly-spaced IQ samples used to estimate a pruned TX port's
// instantaneous power when `runtime.prune_fold_to_awgn` is enabled. Kept small
// so host overhead stays negligible while still tracking silent slots (e.g.
// TDD UL/idle) and differing radio transmit scales without a fixed constant.
constexpr std::size_t kPrunePowerProbes = 64;

inline double db_to_power(double db)
{
  return std::pow(10.0, db / 10.0);
}

inline double power_to_db(double power)
{
  if (power <= 0.0) {
    return -std::numeric_limits<double>::infinity();
  }
  return 10.0 * std::log10(power);
}

// Effective linear power gain of one lane, combining its active TDL tap
// energies, fixed Gain steps (including folded `tx_scale_db`), and live
// PathLoss step. Shared by the CPU and CUDA backends so both evaluate the
// exact same gain before deciding whether to prune a physical link.
inline double lane_effective_power_gain(const ModelConfig& model,
                                        const MutableParams& live,
                                        const PhysicalLinkRuntime& link,
                                        int lane_index)
{
  double gain = 1.0;
  for (const auto& step : model.chain) {
    switch (step.type) {
      case ModelStepType::Tdl: {
        double tap_energy = 0.0;
        if (link.live_matrix_profile_active &&
            lane_index >= 0 && lane_index < link.live_matrix_profile.lane_count) {
          const auto& lp = link.live_matrix_profile.lanes[lane_index];
          for (int t = 0; t < lp.n_taps; ++t) {
            tap_energy += db_to_power(lp.taps[t].gain_db);
          }
        } else if (link.live_profile_active) {
          for (int t = 0; t < link.live_profile.n_taps; ++t) {
            tap_energy += db_to_power(link.live_profile.taps[t].gain_db);
          }
        } else {
          for (std::size_t t = 0; t != step.taps.size(); ++t) {
            const double tap_db = (t == 0) ? static_cast<double>(live.tap0_gain_db)
                                           : step.taps[t].gain_db;
            tap_energy += db_to_power(tap_db);
          }
        }
        gain *= tap_energy;
        break;
      }
      case ModelStepType::Gain: {
        auto it = step.params.find("gain_db");
        const double gain_db = (it == step.params.end()) ? 0.0 : it->second;
        gain *= db_to_power(gain_db);
        break;
      }
      case ModelStepType::PathLoss: {
        gain *= db_to_power(-static_cast<double>(live.path_loss_db));
        break;
      }
      case ModelStepType::Phase:
      case ModelStepType::Cfo:
      case ModelStepType::Awgn:
        break;
    }
  }
  return gain;
}

// Evaluates whether a physical link stays active given its current effective
// power gain in dB, threshold, and hysteresis margin.
inline bool evaluate_pruning_active(bool currently_active,
                                    bool initialized,
                                    double link_gain_db,
                                    double threshold_db,
                                    double hysteresis_db)
{
  if (!initialized || currently_active) {
    return link_gain_db >= threshold_db;
  }
  return link_gain_db >= (threshold_db + hysteresis_db);
}

// Deterministic uniform-stride power estimate for `prune_fold_to_awgn`.
inline double estimate_tx_power_subsampled(std::span<const IqSample> samples)
{
  const std::size_t n = samples.size();
  if (n == 0) {
    return 0.0;
  }
  const std::size_t probes = std::min(n, kPrunePowerProbes);
  double sum = 0.0;
  if (probes == n) {
    for (std::size_t i = 0; i != n; ++i) {
      const double si = samples[i].i;
      const double sq = samples[i].q;
      sum += si * si + sq * sq;
    }
  } else {
    for (std::size_t p = 0; p != probes; ++p) {
      const std::size_t idx = (p * n) / probes;
      const double si = samples[idx].i;
      const double sq = samples[idx].q;
      sum += si * si + sq * sq;
    }
  }
  return sum / static_cast<double>(probes);
}

} // namespace ocg
