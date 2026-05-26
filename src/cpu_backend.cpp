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

// Thin wrapper around the shared `prepare_tdl_state` helper in delay.h --
// kept as a private static so the StepState type stays encapsulated. The
// helper sizes `tdl_polyphase` and the cross-slot `delay_line` ring; no tap
// data is cached here because the kernel reads `step.taps` directly from the
// owning ModelConfig at runtime. When the step has a fading sub-config, also
// draws the deterministic Jakes sub-ray angles / phases / LOS phases into
// state.tdl_fading via the same seed both backends use.
void CpuChannelProcessor::prepare_tdl_step(StepState& state, const ModelStep& step,
                                            std::uint64_t fading_seed)
{
  prepare_tdl_state(step.taps, state.tdl_polyphase, state.delay_line);
  prepare_tdl_fading_state(step, fading_seed, state.tdl_fading);
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
        // Per-link, per-step fading seed -- the CUDA backend computes the
        // same hash so both backends draw the same Jakes sub-ray angles.
        const std::uint64_t fading_seed = static_cast<std::uint64_t>(
            std::hash<std::string>{}(key + ":fading:" + std::to_string(i)));
        prepare_tdl_step(state.steps[i], model.chain[i], fading_seed);
      }
    }
    // Phase 3 v1: populate runtime-mutable params from YAML. Not yet read by
    // the chain execution path -- that wire-in lands in C2. Reference power
    // is unknown at prepare time (depends on actual input); for the initial
    // sigma seed pass 0 so it falls back to the SNR-based formula at execute
    // time (matching the existing chain-step behaviour).
    state.live = populate_mutable_params_from_yaml(model, /*reference_power=*/0.0,
                                                   /*sample_rate_hz=*/0);
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
      case ModelStepType::PathLoss: {
        // Phase 3 C2a: path_loss_db sourced from per-link `live` (populated
        // from YAML at prepare; will be overwritten by snap-from-shadow in
        // C2b once the control plane is wired).
        const float factor = static_cast<float>(std::pow(10.0, -state.live.path_loss_db / 20.0));
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = scale(current[i], factor);
        }
        break;
      }
      case ModelStepType::Awgn: {
        // AWGN sigma derives from runtime input power vs. YAML snr_db (or an
        // explicit noise_power). Not yet sourced from `live` in C2a — the
        // current MutableParams::awgn_sigma representation doesn't capture
        // the SNR-relative-to-current-power semantics. A later commit will
        // rework the struct (e.g. awgn_snr_db) and rewire this path.
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
      case ModelStepType::Phase:
      case ModelStepType::Cfo: {
        // Phase 3 C2a: cfo_hz sourced from per-link `live`. phase_rad stays on
        // the step (not a v1 mutable param).
        const double fixed_phase = param_or(step, "phase_rad", 0.0);
        const double cfo_hz = static_cast<double>(state.live.cfo_hz);
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
      case ModelStepType::Tdl:
        // Delegated to the shared apply_tdl_step / apply_tdl_step_fading
        // helpers in delay.h so the CPU and CUDA backends call literally the
        // same multi-tap convolution. Tap data is read directly from step.taps
        // (the live ModelConfig) -- no per-link cached copy; only the
        // polyphase coefficient set, the cross-slot ring, and (for fading)
        // the per-tap sub-ray seeds live in StepState.
        if (step.fading_enabled) {
          apply_tdl_step_fading(current.data(), next.data(), current.size(),
                                step.taps, step_state.tdl_polyphase,
                                step_state.delay_line, step_state.tdl_fading,
                                sample_rate_hz);
        } else {
          apply_tdl_step(current.data(), next.data(), current.size(),
                         step.taps, step_state.tdl_polyphase,
                         step_state.delay_line);
        }
        break;
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
