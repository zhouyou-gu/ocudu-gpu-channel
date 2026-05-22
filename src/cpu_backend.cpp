#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/delay.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <numbers>
#include <stdexcept>

namespace ocg {
namespace {

double param_or(const ModelStep& step, const std::string& name, double fallback)
{
  auto it = step.params.find(name);
  return it == step.params.end() ? fallback : it->second;
}

double estimate_average_power(std::span<const IqSample> input)
{
  if (input.empty()) {
    return 0.0;
  }
  double sum = 0.0;
  for (const auto& sample : input) {
    sum += power(sample);
  }
  return sum / static_cast<double>(input.size());
}

} // namespace

// One-shot setup for a Tdl step's per-link runtime state. Copies the YAML tap
// list into StepState, precomputes each tap's 8-tap Hamming-windowed sinc
// coefficients from its fractional offset, and sizes the shared delay_line
// ring large enough to feed the highest-delay tap from the previous slot's
// tail. Polyphase coefficients are computed for every tap (including the
// integer-only ones) so the kernel runs an identical inner loop for all
// taps -- sinc(frac=0) collapses to a unit impulse at index 3, so an
// integer-delay tap walks the same filter without a special case.
void CpuChannelProcessor::prepare_tdl_step(StepState& state, const ModelStep& step)
{
  state.tdl_taps = step.taps;
  state.tdl_polyphase.assign(step.taps.size(),
                             std::array<float, kTdlFracFilterTaps>{});
  double max_tau = 0.0;
  for (std::size_t k = 0; k != step.taps.size(); ++k) {
    const double tau = step.taps[k].delay_samples;
    max_tau = std::max(max_tau, tau);
    const double frac = tau - std::floor(tau);
    compute_windowed_sinc_taps(frac, state.tdl_polyphase[k]);
  }
  // History reach: the filter for the largest-tau tap reads back to
  // (n - floor(tau)) + 3 - (kTdlFracFilterTaps - 1) = n - floor(tau) - 4.
  // For n = 0, that's an index of -(floor(max_tau) + 4) into the previous slot.
  const std::size_t ring_size =
      static_cast<std::size_t>(std::ceil(max_tau)) +
      static_cast<std::size_t>(kTdlFracFilterTaps);
  if (state.delay_line.size() < ring_size) {
    state.delay_line.assign(ring_size, IqSample{0.0F, 0.0F});
  }
}

CpuChannelProcessor::LinkState& CpuChannelProcessor::ensure_link_state(const std::string& key,
                                                                       const ModelConfig& model,
                                                                       std::size_t sample_count)
{
  auto it = states_.find(key);
  if (it == states_.end()) {
    // Lazy creation only happens for single-shot use; the broker preallocates
    // every link in prepare() so process_superposition() never inserts
    // concurrently.
    it = states_.emplace(key, LinkState{}).first;
  }

  LinkState& state = it->second;
  if (state.scratch_a.size() < sample_count) {
    state.scratch_a.resize(sample_count);
    state.scratch_b.resize(sample_count);
  }
  if (state.steps.size() != model.chain.size()) {
    state.steps.assign(model.chain.size(), StepState{});
    for (std::size_t i = 0; i != state.steps.size(); ++i) {
      state.steps[i].rng.seed(static_cast<unsigned>(std::hash<std::string>{}(key + ":" + std::to_string(i))));
      if (model.chain[i].type == ModelStepType::Tdl) {
        prepare_tdl_step(state.steps[i], model.chain[i]);
      }
    }
  }
  return state;
}

void CpuChannelProcessor::prepare(const TopologyConfig& config)
{
  for (const auto& link : config.links) {
    const auto* destination = find_device(config, link.to);
    const auto* model = find_model(config, link.model);
    if (destination == nullptr || model == nullptr) {
      continue;
    }
    const std::size_t count = resolve_batch_samples(config.runtime, destination->sample_rate_hz);
    ensure_link_state(link_key(link), *model, count);
  }
  // Receiver-model state, keyed "<node>>rx", so process_superposition can apply
  // a node's noise floor without a concurrent map insert.
  for (const auto& device : config.devices) {
    if (device.rx_model.empty()) {
      continue;
    }
    const auto* model = find_model(config, device.rx_model);
    if (model == nullptr) {
      continue;
    }
    const std::size_t count = resolve_batch_samples(config.runtime, device.sample_rate_hz);
    ensure_link_state(device.id + ">rx", *model, count);
  }
}

void CpuChannelProcessor::apply_chain_to_link(const std::string& link_key_value,
                                              const ModelConfig& model,
                                              std::span<const IqSample> input,
                                              std::span<IqSample> output,
                                              std::uint64_t sample_rate_hz)
{
  if (output.size() != input.size()) {
    throw std::runtime_error("apply_chain_to_link input and output sizes must match");
  }
  if (input.empty()) {
    return;
  }

  LinkState& state = ensure_link_state(link_key_value, model, input.size());

  std::copy(input.begin(), input.end(), state.scratch_a.begin());
  std::span<IqSample> current(state.scratch_a.data(), input.size());
  std::span<IqSample> next(state.scratch_b.data(), input.size());

  for (std::size_t step_index = 0; step_index != model.chain.size(); ++step_index) {
    const auto& step = model.chain[step_index];
    StepState& step_state = state.steps[step_index];

    switch (step.type) {
      case ModelStepType::Gain: {
        const float factor = static_cast<float>(std::pow(10.0, param_or(step, "gain_db", 0.0) / 20.0));
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = scale(current[i], factor);
        }
        break;
      }
      case ModelStepType::PathLoss: {
        const float factor = static_cast<float>(std::pow(10.0, -param_or(step, "path_loss_db", 0.0) / 20.0));
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = scale(current[i], factor);
        }
        break;
      }
      case ModelStepType::Awgn: {
        double noise_power = param_or(step, "noise_power", -1.0);
        if (noise_power < 0.0) {
          const double snr_db = param_or(step, "snr_db", 60.0);
          noise_power = estimate_average_power(current) / std::pow(10.0, snr_db / 10.0);
        }
        const double sigma = std::sqrt(std::max(0.0, noise_power) / 2.0);
        const std::normal_distribution<float>::param_type params(0.0F, static_cast<float>(sigma));
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = {current[i].i + step_state.noise(step_state.rng, params),
                     current[i].q + step_state.noise(step_state.rng, params)};
        }
        break;
      }
      case ModelStepType::IntegerDelay:
      case ModelStepType::FractionalDelay: {
        const double requested_delay = param_or(step, "delay_samples", 0.0);
        const auto integer_delay = static_cast<std::size_t>(std::max(0.0, std::floor(requested_delay)));
        const double fraction =
            step.type == ModelStepType::FractionalDelay ? requested_delay - std::floor(requested_delay) : 0.0;
        apply_sample_delay(current.data(), next.data(), current.size(), integer_delay, fraction,
                           step_state.delay_line);
        break;
      }
      case ModelStepType::Phase:
      case ModelStepType::Cfo: {
        const double fixed_phase = param_or(step, "phase_rad", 0.0);
        const double cfo_hz = param_or(step, "cfo_hz", 0.0);
        const double phase_increment =
            sample_rate_hz == 0 ? 0.0 : 2.0 * std::numbers::pi * cfo_hz / static_cast<double>(sample_rate_hz);
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = rotate(current[i], fixed_phase + step_state.phase_rad);
          step_state.phase_rad += phase_increment;
          if (step_state.phase_rad > 2.0 * std::numbers::pi) {
            step_state.phase_rad = std::fmod(step_state.phase_rad, 2.0 * std::numbers::pi);
          }
        }
        break;
      }
      case ModelStepType::Tdl: {
        // Multi-tap convolution: y[n] = sum_k a_k * x[n - tau_k], with a_k the
        // complex tap weight (gain_db, phase_rad) and tau_k a possibly
        // fractional delay resolved by the per-tap precomputed 8-tap windowed
        // sinc filter. One shared `delay_line` ring carries enough previous-
        // slot tail that even the largest-tau tap's filter reach is satisfied
        // from valid data at the slot boundary.
        //
        // Loop order: per output sample, taps inside. The current slot's input
        // span is walked front-to-back; the M tap-gain coefficients and the
        // M polyphase coefficient sets stay in L1 for the whole inner sweep.
        const auto& taps = step_state.tdl_taps;
        const auto& polyphase = step_state.tdl_polyphase;
        const auto& ring = step_state.delay_line;
        const auto ring_size = static_cast<std::ptrdiff_t>(ring.size());
        // Read input at a possibly-negative index. Non-negative -> current
        // slot's input buffer; negative -> the corresponding tail of `ring`.
        // Indices below -ring_size return zero (pre-stream history).
        const auto read = [&](std::ptrdiff_t idx) -> IqSample {
          if (idx >= 0) {
            return current[static_cast<std::size_t>(idx)];
          }
          const std::ptrdiff_t ring_idx = ring_size + idx;
          if (ring_idx < 0) {
            return IqSample{0.0F, 0.0F};
          }
          return ring[static_cast<std::size_t>(ring_idx)];
        };
        for (std::size_t n = 0; n != current.size(); ++n) {
          IqSample acc{0.0F, 0.0F};
          for (std::size_t k = 0; k != taps.size(); ++k) {
            const TapSpec& tap = taps[k];
            const std::ptrdiff_t tau_int =
                static_cast<std::ptrdiff_t>(std::floor(tap.delay_samples));
            // 8-tap windowed-sinc convolution: y_k = sum_{i=0..7} h[i] * x[n - tau_int + 3 - i].
            // For an integer-only tap the coefficients collapse to a unit
            // impulse at i==3, so this branch handles every tap uniformly.
            const auto& h = polyphase[k];
            IqSample x{0.0F, 0.0F};
            for (int i = 0; i < kTdlFracFilterTaps; ++i) {
              const std::ptrdiff_t read_idx =
                  static_cast<std::ptrdiff_t>(n) - tau_int + 3 - i;
              const IqSample s = read(read_idx);
              x.i += h[static_cast<std::size_t>(i)] * s.i;
              x.q += h[static_cast<std::size_t>(i)] * s.q;
            }
            // Apply complex gain a_k = 10^(gain_db/20) * exp(j*phase_rad).
            const float gain =
                static_cast<float>(std::pow(10.0, tap.gain_db / 20.0));
            const float cos_phi = static_cast<float>(std::cos(tap.phase_rad));
            const float sin_phi = static_cast<float>(std::sin(tap.phase_rad));
            const float rotated_i = x.i * cos_phi - x.q * sin_phi;
            const float rotated_q = x.i * sin_phi + x.q * cos_phi;
            acc.i += gain * rotated_i;
            acc.q += gain * rotated_q;
          }
          next[n] = acc;
        }
        // Roll the ring forward: the new tail is the last `ring.size()` samples
        // of the current slot's input. When the slot is shorter than the ring,
        // shift the existing tail left by `current.size()` and append all of
        // the current slot at the end.
        std::vector<IqSample>& mutable_ring = step_state.delay_line;
        const std::size_t dl_size = mutable_ring.size();
        if (current.size() >= dl_size) {
          std::copy(current.end() - static_cast<std::ptrdiff_t>(dl_size),
                    current.end(), mutable_ring.begin());
        } else {
          const std::size_t keep_old = dl_size - current.size();
          std::move(mutable_ring.begin() +
                        static_cast<std::ptrdiff_t>(current.size()),
                    mutable_ring.end(), mutable_ring.begin());
          std::copy(current.begin(), current.end(),
                    mutable_ring.begin() +
                        static_cast<std::ptrdiff_t>(keep_old));
        }
        break;
      }
    }
    std::swap(current, next);
  }

  std::copy(current.begin(), current.end(), output.begin());
}

void CpuChannelProcessor::process_superposition(const std::string& dst_key,
                                                const std::vector<SuperpositionInput>& inputs,
                                                const ModelConfig* rx_model,
                                                std::uint64_t sample_rate_hz,
                                                std::span<IqSample> output)
{
  // Reference superposition: shape each incoming edge through its own model,
  // then sum. The CUDA backend fuses this into one kernel; here it stays a
  // plain loop so it can serve as the correctness reference.
  std::fill(output.begin(), output.end(), IqSample{});
  // Reused across calls on this thread (the broker runs one server thread per
  // destination node), so a serve does not allocate.
  thread_local IqBuffer scratch;
  if (scratch.size() < output.size()) {
    scratch.resize(output.size());
  }
  const std::span<IqSample> shaped(scratch.data(), output.size());
  for (const auto& edge : inputs) {
    if (edge.model == nullptr || edge.samples.size() != output.size()) {
      throw std::runtime_error("CPU superposition input is malformed");
    }
    apply_chain_to_link(edge.link_key, *edge.model, edge.samples, shaped, sample_rate_hz);
    for (std::size_t s = 0; s != output.size(); ++s) {
      output[s] += scratch[s];
    }
  }
  // Receiver model (noise floor) applied once to the summed signal.
  if (rx_model != nullptr) {
    const std::span<const IqSample> summed(output.data(), output.size());
    apply_chain_to_link(dst_key + ">rx", *rx_model, summed, output, sample_rate_hz);
  }
}

} // namespace ocg
