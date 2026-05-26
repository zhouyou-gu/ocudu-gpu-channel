// Phase 3 C5: runtime-update parity test.
//
// Drives a scripted sequence of shadow writes against both the CPU and
// (when built with CUDA) CUDA backends and asserts that:
//   1. The same update sequence on both backends produces matching output
//      slot-by-slot at the project's 1e-3 parity tolerance.
//   2. Output actually changes after each update — proving the snap
//      mechanism in fact threaded the new value into the kernel.
//
// The test bypasses ControlServer and writes directly to BrokerLinkControl::
// shadow so the test does not depend on the ZMQ wire layer; ControlServer
// is already covered by tests/test_control_server.cpp.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/runtime_control.h"

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#if OCUDU_GPU_CHANNEL_HAS_CUDA
#include "ocudu_gpu_channel/cuda_backend.h"
#include "ocudu_gpu_channel/backend.h"
#endif

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

constexpr float kParityTolerance = 1e-3F;

bool near_float(float a, float b, float tol = kParityTolerance)
{
  return std::fabs(a - b) <= tol;
}

float max_abs_diff(const std::vector<ocg::IqSample>& a,
                   const std::vector<ocg::IqSample>& b)
{
  float m = 0.0F;
  for (std::size_t i = 0; i < a.size(); ++i) {
    m = std::max(m, std::fabs(a[i].i - b[i].i));
    m = std::max(m, std::fabs(a[i].q - b[i].q));
  }
  return m;
}

// Build a minimal 1-link topology: one gNB → one UE, one tdl (unit gain
// pass-through) followed by a path_loss step. path_loss_db is the v1
// mutable param under test.
ocg::TopologyConfig make_topology(ocg::Backend backend)
{
  ocg::TopologyConfig cfg;
  cfg.runtime.backend = backend;
  cfg.runtime.batch_samples_auto = false;
  cfg.runtime.batch_samples = 8;
  cfg.runtime.queue_samples = 64;
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
  ocg::ModelStep pl;
  pl.type = ocg::ModelStepType::PathLoss;
  pl.params["path_loss_db"] = 0.0;  // YAML default; will be runtime-updated
  m.chain.push_back(pl);
  cfg.models.emplace(m.id, m);
  return cfg;
}

// Run one slot through `proc` and return the output buffer.
std::vector<ocg::IqSample> run_slot(ocg::ChannelProcessor& proc,
                                    const ocg::ModelConfig& model,
                                    const std::vector<ocg::IqSample>& input)
{
  std::vector<ocg::IqSample> output(input.size(), {0.0F, 0.0F});
  const std::span<const ocg::IqSample> input_span(input.data(), input.size());
  std::vector<ocg::SuperpositionInput> edges;
  edges.push_back({.link_key = "gnb0>ue0", .model = &model, .samples = input_span});
  proc.process_superposition("ue0", edges, nullptr,
                             23040000ULL, std::span<ocg::IqSample>(output.data(), output.size()));
  return output;
}

// Bump shadow + seqno on the link "gnb0>ue0" — simulates a control-plane
// write without involving ControlServer.
void write_shadow_path_loss(ocg::ChannelProcessor& proc, float new_db)
{
  auto map = proc.collect_control_links();
  auto it = map.find("gnb0>ue0");
  require(it != map.end() && it->second != nullptr, "link found in control map");
  it->second->shadow.path_loss_db = new_db;
  it->second->seqno.fetch_add(1, std::memory_order_release);
}

}  // namespace

int main()
{
  const std::vector<ocg::IqSample> input = {
      {1.0F, 0.0F}, {0.0F, 1.0F}, {1.0F, 1.0F}, {0.5F, -0.5F},
      {-1.0F, 0.0F}, {0.0F, -1.0F}, {1.0F, -1.0F}, {-0.5F, 0.5F}};

  // ── CPU backend: drive 3 slots with path_loss updates between them ─────
  auto cpu_cfg = make_topology(ocg::Backend::Cpu);
  ocg::CpuChannelProcessor cpu;
  cpu.prepare(cpu_cfg);
  const ocg::ModelConfig& cpu_model = cpu_cfg.models["chain"];

  // Slot 0: YAML default (path_loss_db = 0 → unit gain)
  auto out0_cpu = run_slot(cpu, cpu_model, input);
  require(near_float(out0_cpu[0].i, 1.0F), "slot 0 CPU: unit gain on sample 0 I");
  require(near_float(out0_cpu[0].q, 0.0F), "slot 0 CPU: unit gain on sample 0 Q");

  // Update path_loss to -20 dB → 10x gain. (Negative path_loss_db = gain
  // in this code path; the factor is 10^(-path_loss_db/20).)
  write_shadow_path_loss(cpu, -20.0F);
  auto out1_cpu = run_slot(cpu, cpu_model, input);
  require(near_float(out1_cpu[0].i, 10.0F),
          "slot 1 CPU: -20 dB path_loss → 10x amplitude");
  require(near_float(out1_cpu[3].q, -5.0F),
          "slot 1 CPU: 10x scaling preserves Q sign + magnitude");

  // Update to +20 dB → 0.1x gain.
  write_shadow_path_loss(cpu, 20.0F);
  auto out2_cpu = run_slot(cpu, cpu_model, input);
  require(near_float(out2_cpu[0].i, 0.1F),
          "slot 2 CPU: +20 dB path_loss → 0.1x amplitude");

#if OCUDU_GPU_CHANNEL_HAS_CUDA
  if (ocg::cuda_compiled()) {
    // ── CUDA backend: same script, expect bit-similar outputs ────────────
    auto cuda_cfg = make_topology(ocg::Backend::Cuda);
    auto cuda = ocg::create_channel_processor(cuda_cfg);  // calls prepare()
    const ocg::ModelConfig& cuda_model = cuda_cfg.models["chain"];

    auto out0_cuda = run_slot(*cuda, cuda_model, input);
    require(max_abs_diff(out0_cpu, out0_cuda) <= kParityTolerance,
            "slot 0 CPU↔CUDA parity within 1e-3");

    write_shadow_path_loss(*cuda, -20.0F);
    auto out1_cuda = run_slot(*cuda, cuda_model, input);
    require(max_abs_diff(out1_cpu, out1_cuda) <= kParityTolerance,
            "slot 1 CPU↔CUDA parity within 1e-3 after first runtime update");

    write_shadow_path_loss(*cuda, 20.0F);
    auto out2_cuda = run_slot(*cuda, cuda_model, input);
    require(max_abs_diff(out2_cpu, out2_cuda) <= kParityTolerance,
            "slot 2 CPU↔CUDA parity within 1e-3 after second runtime update");
  }
#endif

  std::cout << "test_runtime_update_parity OK\n";
  return 0;
}
