#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/cuda_backend.h"
#include <memory>
#include <sstream>
#include <stdexcept>

// Backend selection. This translation unit is backend-agnostic glue: it knows
// which model steps the GPU backend supports and which concrete backend to
// build, but contains no channel-processing code itself.

namespace ocg {
namespace {

// Per-sample steps the GPU runs unconditionally, anywhere in a chain.
bool cuda_step_supported(ModelStepType type)
{
  return type == ModelStepType::Gain || type == ModelStepType::PathLoss || type == ModelStepType::Phase ||
         type == ModelStepType::Cfo || type == ModelStepType::Awgn;
}

// Per-sample steps the CPU backend's chain loop knows how to execute. Mirrors
// `cuda_step_supported` so a topology with an unimplemented step (currently
// only `tdl`) fails at processor build time on either backend, not deep inside
// the first slot's process loop.
bool cpu_step_supported(ModelStepType type)
{
  return type == ModelStepType::Gain || type == ModelStepType::PathLoss || type == ModelStepType::Phase ||
         type == ModelStepType::Cfo || type == ModelStepType::Awgn ||
         type == ModelStepType::IntegerDelay || type == ModelStepType::FractionalDelay;
}

bool is_sample_delay(ModelStepType type)
{
  return type == ModelStepType::IntegerDelay || type == ModelStepType::FractionalDelay;
}

} // namespace

std::vector<std::string> validate_cpu_support(const TopologyConfig& config)
{
  // Symmetric to validate_cuda_support: refuse a topology whose chain references
  // a step the CPU backend has no implementation for. Today the only such step
  // is `tdl` (Phase 1.0 lands the schema, Phase 1.1 lands the kernel). Sample-
  // delay steps are accepted at any position on CPU -- the CPU loop materialises
  // intermediates between steps so a mid-chain delay is well-defined here.
  std::vector<std::string> errors;
  for (const auto& link : config.links) {
    const auto* model = find_model(config, link.model);
    if (model == nullptr) {
      continue;
    }
    for (const auto& step : model->chain) {
      if (!cpu_step_supported(step.type)) {
        errors.emplace_back("CPU backend does not support model " + model->id + " step " +
                            to_string(step.type) + " yet");
      }
    }
  }
  for (const auto& device : config.devices) {
    if (device.rx_model.empty()) {
      continue;
    }
    const auto* rx = find_model(config, device.rx_model);
    if (rx == nullptr) {
      continue;
    }
    for (const auto& step : rx->chain) {
      if (!cpu_step_supported(step.type)) {
        errors.emplace_back("CPU backend does not support receiver model " + rx->id + " step " +
                            to_string(step.type) + " yet");
      }
    }
  }
  return errors;
}

std::vector<std::string> validate_cuda_support(const TopologyConfig& config)
{
  // The CUDA backend applies a propagation delay host-side while staging, so it
  // needs the raw link input -- it can run a sample-delay step only when that
  // step leads the chain (a non-leading delay would need a mid-chain
  // intermediate the per-sample kernel never materialises). A leading delay is
  // the physically natural order anyway: the signal propagates, then the
  // receiver-side effects (CFO, noise) apply.
  std::vector<std::string> errors;
  for (const auto& link : config.links) {
    const auto* model = find_model(config, link.model);
    if (model == nullptr) {
      continue;
    }
    for (std::size_t s = 0; s != model->chain.size(); ++s) {
      const auto& step = model->chain[s];
      if (is_sample_delay(step.type)) {
        if (s != 0) {
          errors.emplace_back("CUDA backend supports model " + model->id + " step " + to_string(step.type) +
                              " only as the first step of a chain");
        }
        continue;
      }
      if (!cuda_step_supported(step.type)) {
        errors.emplace_back("CUDA backend does not support model " + model->id + " step " + to_string(step.type));
      }
    }
  }

  // Receiver models run on the summed signal with no per-link input, so a
  // sample delay there is meaningless and is never applied -- reject it.
  for (const auto& device : config.devices) {
    if (device.rx_model.empty()) {
      continue;
    }
    const auto* rx = find_model(config, device.rx_model);
    if (rx == nullptr) {
      continue;
    }
    for (const auto& step : rx->chain) {
      if (is_sample_delay(step.type)) {
        errors.emplace_back("CUDA backend does not support a delay step in receiver model " + rx->id);
      } else if (!cuda_step_supported(step.type)) {
        errors.emplace_back("CUDA backend does not support receiver model " + rx->id + " step " +
                            to_string(step.type));
      }
    }
  }
  return errors;
}

std::unique_ptr<ChannelProcessor> create_channel_processor(const TopologyConfig& config)
{
  if (config.runtime.backend == Backend::Cpu) {
    auto cpu_errors = validate_cpu_support(config);
    if (!cpu_errors.empty()) {
      std::ostringstream oss;
      oss << "invalid CPU topology:";
      for (const auto& error : cpu_errors) {
        oss << "\n- " << error;
      }
      throw std::runtime_error(oss.str());
    }
    auto processor = std::make_unique<CpuChannelProcessor>();
    processor->prepare(config);
    return processor;
  }

  // CUDA is the default/primary backend; refuse a topology it cannot run
  // rather than silently degrading.
  auto cuda_errors = validate_cuda_support(config);
  if (!cuda_errors.empty()) {
    std::ostringstream oss;
    oss << "invalid CUDA topology:";
    for (const auto& error : cuda_errors) {
      oss << "\n- " << error;
    }
    throw std::runtime_error(oss.str());
  }

#if OCUDU_GPU_CHANNEL_HAS_CUDA
  auto processor = make_cuda_processor(config);
  processor->prepare(config);
  return processor;
#else
  throw std::runtime_error("topology requested CUDA backend, but this build was not compiled with CUDA");
#endif
}

} // namespace ocg
