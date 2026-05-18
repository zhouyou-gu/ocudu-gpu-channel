#include "ocudu_gpu_channel/backend.h"
#if OCUDU_GPU_CHANNEL_HAS_CUDA
#include "ocudu_gpu_channel/cuda_backend.h"
#endif

namespace ocg {

bool cuda_compiled()
{
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  return cuda_runtime_probe();
#else
  return false;
#endif
}

std::string backend_status()
{
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  return cuda_compiled() ? "cuda:compiled,runtime:available" : "cuda:compiled,runtime:unavailable";
#else
  return "cuda:not-compiled";
#endif
}

} // namespace ocg
