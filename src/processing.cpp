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

bool cuda_step_supported(ModelStepType type)
{
  return type == ModelStepType::Gain || type == ModelStepType::PathLoss || type == ModelStepType::Phase ||
         type == ModelStepType::Cfo || type == ModelStepType::Awgn;
}

} // namespace

std::vector<std::string> validate_cuda_support(const TopologyConfig& config)
{
  std::vector<std::string> errors;
  for (const auto& link : config.links) {
    const auto* model = find_model(config, link.model);
    if (model == nullptr) {
      continue;
    }
    for (const auto& step : model->chain) {
      if (!cuda_step_supported(step.type)) {
        errors.emplace_back("CUDA backend does not support model " + model->id + " step " + to_string(step.type));
      }
    }
  }
  return errors;
}

std::unique_ptr<ChannelProcessor> create_channel_processor(const TopologyConfig& config)
{
  if (config.runtime.backend == Backend::Cpu) {
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
