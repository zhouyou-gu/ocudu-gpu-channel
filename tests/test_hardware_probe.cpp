// v3.2: tests for the CUDA hardware probe + footprint estimate +
// kernel-target SM accessor. On CPU-only macOS builds the probe
// returns ok=false cleanly with a typed error; the footprint estimate
// returns 0; kernel_target_sm() returns {0,0}. On CUDA-enabled
// Linux builds (GPU workstation) the probe returns sane values
// (smoke-tested in the next test below — gated on cuda_compiled()).

#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/config.h"

#include <cstdlib>
#include <iostream>
#include <string>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

ocg::TopologyConfig make_minimal_cuda_topology()
{
  ocg::TopologyConfig cfg;
  cfg.runtime.backend = ocg::Backend::Cuda;
  cfg.runtime.batch_samples_auto = false;
  cfg.runtime.batch_samples = 23040;
  cfg.runtime.queue_samples = 184320;
  cfg.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
       .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
      {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
       .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
  cfg.links = {{.from = "gnb0", .to = "ue0", .model = "chain"}};
  ocg::ModelConfig m;
  m.id = "chain";
  m.chain.push_back({.type = ocg::ModelStepType::Tdl,
                     .params = {},
                     .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                     .taps_declared = true});
  cfg.models.emplace(m.id, m);
  return cfg;
}

}  // namespace

int main()
{
  // ── Case 1: probe behaviour matches the build's compile-time CUDA
  //           status. The function must always return a sane struct
  //           and never throw — it is called unconditionally at
  //           broker startup, including on CPU-only builds.
  {
    const auto p = ocg::probe_cuda_hardware(0);
    if (!ocg::cuda_compiled()) {
      require(!p.ok, "CPU-only build → probe.ok must be false");
      require(!p.error.empty(), "CPU-only build → probe.error must be populated");
      require(p.device_id == 0, "probe echoes the requested device_id");
    } else {
      // GPU build: at least one device must be visible AND the
      // returned struct's fields must be plausible. If we're on a
      // GPU workstation, this confirms the probe wiring.
      require(p.ok || !p.error.empty(),
              "GPU build → probe.ok=true OR probe.error explains why not");
      if (p.ok) {
        require(!p.name.empty(), "probe.name should be non-empty");
        require(p.sm_major > 0, "probe.sm_major should be > 0");
        require(p.total_mem_bytes > 0, "probe.total_mem_bytes should be > 0");
      }
    }
  }

  // ── Case 2: kernel_target_sm() returns the CMake target on a CUDA
  //           build, or {0,0} on a CPU-only build.
  {
    const auto t = ocg::kernel_target_sm();
    if (!ocg::cuda_compiled()) {
      require(t.major == 0 && t.minor == 0,
              "CPU-only build → kernel_target_sm should be {0,0}");
    } else {
      require(t.major > 0,
              "GPU build → kernel_target_sm.major should be > 0 "
              "(set via OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES)");
    }
  }

  // ── Case 3: footprint estimate. On CPU-only builds the helper
  //           returns 0 (no CUDA allocations modelled). On GPU
  //           builds it returns a positive value for any non-trivial
  //           topology.
  {
    auto cfg = make_minimal_cuda_topology();
    const std::uint64_t footprint = ocg::estimate_cuda_device_footprint_bytes(cfg);
#if OCUDU_GPU_CHANNEL_HAS_CUDA
    require(footprint > 0, "GPU build → 1-link topology should estimate > 0 bytes");
    // Sanity: 23 040 samples × 8 B = 184 320 B per buffer × ~3 buffers
    // (staged + output + source_iq) ≈ 552 960 B floor. Allow generous
    // upper bound to absorb DeviceLinkState + steps + meta.
    require(footprint < 10ULL * 1024ULL * 1024ULL,
            "1-link topology should fit comfortably in 10 MiB");
#else
    require(footprint == 0, "CPU-only build → estimate should return 0");
#endif
  }

  std::cout << "test_hardware_probe OK\n";
  return 0;
}
