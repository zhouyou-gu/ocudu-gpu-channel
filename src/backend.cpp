#include "ocudu_gpu_channel/backend.h"
#if OCUDU_GPU_CHANNEL_HAS_CUDA
#include "ocudu_gpu_channel/cuda_backend.h"
#include "ocudu_gpu_channel/device_channel.h"
#endif

#include <algorithm>
#include <unordered_map>

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

#if !OCUDU_GPU_CHANNEL_HAS_CUDA
// CPU-only build stub. The real probe lives in cuda_backend.cu when the
// library is built with CUDA.
HardwareProbe probe_cuda_hardware(int device_id)
{
  HardwareProbe p;
  p.device_id = device_id;
  p.ok = false;
  p.error = "cuda not compiled into this build";
  return p;
}
#endif

KernelTargetSm kernel_target_sm()
{
#if OCUDU_GPU_CHANNEL_HAS_CUDA && defined(OCUDU_GPU_CHANNEL_KERNEL_SM)
  // Build system passes OCUDU_GPU_CHANNEL_KERNEL_SM as a 3-digit int
  // like 120 → SM 12.0. CMake variable
  // OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES (default "120") wins through
  // a target_compile_definitions hop wired in CMakeLists.txt.
  KernelTargetSm out;
  const int s = OCUDU_GPU_CHANNEL_KERNEL_SM;
  out.major = s / 10;
  out.minor = s % 10;
  return out;
#else
  return {};
#endif
}

// v3.2: estimate device-side memory footprint for a given topology.
// Mirrors the CUDA backend's prepare() allocation plan from
// cuda_backend.cu (process_superposition state, per-link
// DeviceLinkState, per-source IQ slot).
std::uint64_t estimate_cuda_device_footprint_bytes(const TopologyConfig& config)
{
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  std::uint64_t total = 0;

  // Per-destination superpose-state allocations. Walk every device and
  // count incoming edges (links whose `to` matches the device).
  for (const auto& dst : config.devices) {
    std::uint64_t incoming = 0;
    std::uint64_t max_steps = 0;
    std::unordered_map<std::string, int> src_to_index;
    for (const auto& link : config.links) {
      if (link.to != dst.id) continue;
      ++incoming;
      const auto* m = find_model(config, link.model);
      if (m) {
        max_steps = std::max<std::uint64_t>(max_steps, m->chain.size());
      }
      src_to_index.try_emplace(link.from, 0);
    }
    if (incoming == 0) continue;

    const std::uint64_t count = resolve_batch_samples(config.runtime, dst.sample_rate_hz);
    const std::uint64_t iq_bytes = 8;        // sizeof(IqSample) = 2 * float

    // device_staged + device_output + device_steps + device_step_meta
    // + per-edge DeviceLinkState + device_source_iq.
    total += incoming * count * iq_bytes;                   // device_staged
    total += count * iq_bytes;                              // device_output
    total += incoming * std::max<std::uint64_t>(1, max_steps) * 24;  // GpuStep ≈ 24 B
    total += 2 * incoming * sizeof(int);                    // device_step_meta
    total += incoming * sizeof(DeviceLinkState);            // device_link_states
    total += static_cast<std::uint64_t>(src_to_index.size())
             * count * iq_bytes;                            // device_source_iq
  }
  return total;
#else
  (void)config;
  return 0;
#endif
}

} // namespace ocg
