#pragma once

#include "ocudu_gpu_channel/config.h"

#include <cstdint>
#include <string>

namespace ocg {

bool cuda_compiled();
std::string backend_status();

// v3.2: typed result of a startup-time CUDA hardware probe. When the
// runtime requests the CUDA backend, the broker fills this from
// cudaGetDeviceProperties before constructing the processor, logs an
// event=hardware_probe line, and rejects startup on memory-footprint
// overrun or (if --hardware-strict is set) SM-capability mismatch.
//
// `ok == false` means CUDA is not available on this build/runtime
// (CPU-only build, or `cudaGetDeviceCount` returned 0); `error` carries
// a human-readable reason in that case. Other fields are valid only
// when ok == true.
struct HardwareProbe {
  bool          ok = false;
  std::string   error;                  // populated when ok == false
  int           device_id = -1;
  std::string   name;                   // e.g. "NVIDIA GeForce RTX 5090"
  int           sm_major = 0;
  int           sm_minor = 0;
  std::uint64_t total_mem_bytes = 0;
  int           driver_version = 0;     // e.g. 12080
  int           runtime_version = 0;    // e.g. 12080
  // pcie_link_gen / pcie_link_width are deliberately NOT included.
  // The CUDA Runtime API has no clean attribute for them; reporting
  // them accurately requires NVML, which would add a runtime
  // dependency. Until the user opts into NVML the broker stays honest
  // about the limitation rather than zero-filling fields that imply a
  // measurement.
};

// Probe `device_id` (typically `config.runtime.gpu_device`). Safe to
// call on CPU-only builds; returns ok=false with error="cuda not
// compiled into this build".
HardwareProbe probe_cuda_hardware(int device_id);

// v3.2: best-effort estimate of the device-side memory footprint the
// CUDA backend will allocate for `config`. Sums per-destination
// device_staged + device_output + device_link_states + device_source_iq
// + device_steps + device_step_meta. Conservative — ignores ancillary
// CUDA runtime allocations, so the actual usage will be slightly
// higher. Used by the broker startup to reject a topology that would
// exceed `HardwareProbe::total_mem_bytes`.
std::uint64_t estimate_cuda_device_footprint_bytes(const TopologyConfig& config);

// v3.2: kernel-target SM "major.minor" the CUDA backend was compiled
// for. Reads OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES from build defines;
// returns {0,0} on CPU-only builds. Used by --hardware-strict to
// reject startup when the runtime SM is below the kernel's target.
struct KernelTargetSm { int major = 0; int minor = 0; };
KernelTargetSm kernel_target_sm();

} // namespace ocg
