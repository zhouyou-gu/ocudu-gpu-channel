#pragma once

#include "ocudu_gpu_channel/processing.h"
#include <memory>

// CUDA channel-processing backend — the project's primary, scale-out target:
// GPU-accelerated channel emulation for many concurrent gNB/UE links at OCUDU
// ZMQ sample rates. The entry points below are defined in cuda_backend.cu and
// only compiled when the build has CUDA (OCUDU_GPU_CHANNEL_HAS_CUDA); callers
// must guard their use behind that macro.

namespace ocg {

// True if a usable CUDA device is present at runtime.
bool cuda_runtime_probe();

// Builds the GPU channel processor for the given topology.
std::unique_ptr<ChannelProcessor> make_cuda_processor(const TopologyConfig& config);

} // namespace ocg
