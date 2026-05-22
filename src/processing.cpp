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

// Per-sample steps the GPU runs unconditionally, anywhere in a chain. The
// leading-propagation step (`tdl`) lives outside this set -- it ran host-side
// during staging and is emitted as a no-op pass-through GpuStep on the device
// side.
bool cuda_step_supported(ModelStepType type)
{
  return type == ModelStepType::PathLoss || type == ModelStepType::Phase ||
         type == ModelStepType::Cfo || type == ModelStepType::Awgn;
}

// Steps the CPU backend's chain loop knows how to execute. `tdl` covers what
// the old gain / integer_delay / fractional_delay steps used to express, as a
// single-tap tdl with the corresponding delay and complex gain folded in.
bool cpu_step_supported(ModelStepType type)
{
  return type == ModelStepType::PathLoss || type == ModelStepType::Phase ||
         type == ModelStepType::Cfo || type == ModelStepType::Awgn ||
         type == ModelStepType::Tdl;
}

// The step that must lead the chain on the CUDA backend because the per-sample
// kernel cannot materialise it inline. It runs host-side during staging
// (apply_tdl_step in delay.h) and is a no-op pass-through on the device.
// Phase 1.3 collapsed the previous three-way list (integer_delay,
// fractional_delay, tdl) down to just tdl.
bool is_leading_propagation(ModelStepType type)
{
  return type == ModelStepType::Tdl;
}

} // namespace

std::vector<std::string> validate_cpu_support(const TopologyConfig& config)
{
  // Symmetric to validate_cuda_support: refuse a topology whose chain references
  // a step the CPU backend has no implementation for. Today the CPU loop
  // handles every step type at any chain position, so this returns empty for
  // any well-formed topology -- it stays defensive against a future step type
  // that lands on CUDA before the CPU reference catches up.
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
  // The CUDA backend applies leading-propagation work host-side while staging,
  // so it needs the raw link input -- it can run a sample delay or a `tdl`
  // step only when that step leads the chain (a non-leading one would need a
  // mid-chain intermediate the per-sample kernel never materialises). A
  // leading propagation step is the physically natural order anyway: the
  // signal propagates, then the receiver-side effects (CFO, noise) apply.
  std::vector<std::string> errors;
  for (const auto& link : config.links) {
    const auto* model = find_model(config, link.model);
    if (model == nullptr) {
      continue;
    }
    for (std::size_t s = 0; s != model->chain.size(); ++s) {
      const auto& step = model->chain[s];
      if (is_leading_propagation(step.type)) {
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

  // Receiver models run on the summed signal with no per-link input, so any
  // leading-propagation step (sample delay or tdl) there is meaningless and
  // is never applied -- reject it.
  for (const auto& device : config.devices) {
    if (device.rx_model.empty()) {
      continue;
    }
    const auto* rx = find_model(config, device.rx_model);
    if (rx == nullptr) {
      continue;
    }
    for (const auto& step : rx->chain) {
      if (is_leading_propagation(step.type)) {
        errors.emplace_back("CUDA backend does not support a " + to_string(step.type) +
                            " step in receiver model " + rx->id);
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
