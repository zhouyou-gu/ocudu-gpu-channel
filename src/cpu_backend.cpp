#include "ocudu_gpu_channel/cpu_backend.h"
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

CpuChannelProcessor::LinkState& CpuChannelProcessor::ensure_link_state(const std::string& key,
                                                                       const ModelConfig& model,
                                                                       std::size_t sample_count)
{
  auto it = states_.find(key);
  if (it == states_.end()) {
    // Lazy creation only happens for single-shot use; the broker preallocates
    // every link in prepare() so process_into() never inserts concurrently.
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

void CpuChannelProcessor::process_into(const std::string& link_key_value,
                                       const ModelConfig& model,
                                       std::span<const IqSample> input,
                                       std::span<IqSample> output,
                                       std::uint64_t sample_rate_hz)
{
  if (output.size() != input.size()) {
    throw std::runtime_error("process_into input and output sizes must match");
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
        const std::size_t history_size = integer_delay + 2;
        if (step_state.delay_line.size() < history_size) {
          step_state.delay_line.insert(step_state.delay_line.begin(),
                                       history_size - step_state.delay_line.size(), {});
        }

        auto sample_at = [&](std::ptrdiff_t index) {
          if (index >= 0) {
            return current[static_cast<std::size_t>(index)];
          }
          return step_state.delay_line[static_cast<std::size_t>(
              static_cast<std::ptrdiff_t>(step_state.delay_line.size()) + index)];
        };

        for (std::size_t n = 0; n != current.size(); ++n) {
          const auto delayed_index = static_cast<std::ptrdiff_t>(n) - static_cast<std::ptrdiff_t>(integer_delay);
          const IqSample sample0 = sample_at(delayed_index);
          const IqSample sample1 = sample_at(delayed_index - 1);
          next[n].i = static_cast<float>((1.0 - fraction) * sample0.i + fraction * sample1.i);
          next[n].q = static_cast<float>((1.0 - fraction) * sample0.q + fraction * sample1.q);
        }

        if (current.size() >= history_size) {
          step_state.delay_line.assign(current.end() - static_cast<std::ptrdiff_t>(history_size), current.end());
        } else {
          const std::size_t keep_old = history_size - current.size();
          std::move(step_state.delay_line.end() - static_cast<std::ptrdiff_t>(keep_old), step_state.delay_line.end(),
                    step_state.delay_line.begin());
          std::copy(current.begin(), current.end(),
                    step_state.delay_line.begin() + static_cast<std::ptrdiff_t>(keep_old));
        }
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
    }
    std::swap(current, next);
  }

  std::copy(current.begin(), current.end(), output.begin());
}

IqBuffer CpuChannelProcessor::process(const std::string& link_key_value,
                                      const ModelConfig& model,
                                      const IqBuffer& input,
                                      std::uint64_t sample_rate_hz)
{
  IqBuffer output(input.size());
  process_into(link_key_value, model, input, output, sample_rate_hz);
  return output;
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
    process_into(edge.link_key, *edge.model, edge.samples, shaped, sample_rate_hz);
    for (std::size_t s = 0; s != output.size(); ++s) {
      output[s] += scratch[s];
    }
  }
  // Receiver model (noise floor) applied once to the summed signal.
  if (rx_model != nullptr) {
    const std::span<const IqSample> summed(output.data(), output.size());
    process_into(dst_key + ">rx", *rx_model, summed, output, sample_rate_hz);
  }
}

} // namespace ocg
