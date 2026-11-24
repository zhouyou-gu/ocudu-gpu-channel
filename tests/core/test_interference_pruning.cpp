// Interference pruning unit, parity, hysteresis, reactivation, and AWGN-folding tests.
//
// Exercises:
//   1. Config parsing and validation (including rejection + negative controls).
//   2. Static 1x1 multi-cell pruning accuracy, active_lanes accounting, and
//      negative control (over-pruning a strong interferer changes output).
//   3. All-edges-pruned zero output and receiver-model preservation.
//   4. 2x2 MIMO physical-link pruning with CPU/CUDA parity across both RX rows.
//   5. Dynamic path_loss_db and profile_swap transitions with hysteresis,
//      re-activation delay-line reset, and CPU/CUDA parity.
//   6. Residual AWGN folding (`prune_fold_to_awgn`) analytic variance check
//      and silent-slot zero-power check on both CPU and CUDA.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/interference_pruning.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/runtime_control.h"

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <unistd.h>
#include <vector>

#if OCUDU_GPU_CHANNEL_HAS_CUDA
#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/cuda_backend.h"
#endif

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
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

double avg_power(const std::vector<ocg::IqSample>& samples)
{
  if (samples.empty()) {
    return 0.0;
  }
  double sum = 0.0;
  for (const auto& s : samples) {
    sum += static_cast<double>(s.i) * s.i + static_cast<double>(s.q) * s.q;
  }
  return sum / static_cast<double>(samples.size());
}

std::vector<ocg::IqSample> make_signal(std::size_t count, float freq_scale, float phase_offset)
{
  std::vector<ocg::IqSample> out(count);
  for (std::size_t i = 0; i != count; ++i) {
    const float angle = freq_scale * static_cast<float>(i) + phase_offset;
    out[i] = {std::cos(angle), std::sin(angle)};
  }
  return out;
}

ocg::ModelConfig make_tdl_pathloss_model(const std::string& id, double path_loss_db,
                                         double tap0_delay = 0.0, double tap0_gain_db = 0.0)
{
  ocg::ModelConfig m;
  m.id = id;
  ocg::ModelStep tdl;
  tdl.type = ocg::ModelStepType::Tdl;
  tdl.taps_declared = true;
  tdl.taps.push_back({.delay_samples = tap0_delay, .gain_db = tap0_gain_db, .phase_rad = 0.0});
  m.chain.push_back(std::move(tdl));
  ocg::ModelStep pl;
  pl.type = ocg::ModelStepType::PathLoss;
  pl.params["path_loss_db"] = path_loss_db;
  m.chain.push_back(std::move(pl));
  return m;
}

ocg::ModelConfig make_rx_noise_model(const std::string& id, double noise_power)
{
  ocg::ModelConfig m;
  m.id = id;
  ocg::ModelStep awgn;
  awgn.type = ocg::ModelStepType::Awgn;
  awgn.params["noise_power"] = noise_power;
  m.chain.push_back(std::move(awgn));
  return m;
}

bool has_cuda_gpu()
{
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  return ocg::cuda_runtime_probe();
#else
  return false;
#endif
}

std::string write_temp_yaml(const std::string& body)
{
  static int seq = 0;
  const std::string path =
      "/tmp/ocg_test_prune_" + std::to_string(getpid()) + "_" + std::to_string(++seq) + ".yaml";
  std::ofstream out(path);
  out << body;
  out.close();
  return path;
}

void test_config_parsing_and_validation()
{
  const std::string base_devices_and_models = R"(
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:4000
    rx_endpoint: tcp://*:4001
    rx_model: rx_noise
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:4002
    rx_endpoint: tcp://*:4003
    rx_model: rx_noise
links:
  - from: gnb0
    to: ue0
    model: m0
  - from: ue0
    to: gnb0
    model: m0
models:
  m0:
    chain:
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
  rx_noise:
    chain:
      - type: awgn
        noise_power: 1.0e-6
)";

  // 1. Valid pruning config with hysteresis and AWGN folding.
  {
    const std::string path = write_temp_yaml(
        "runtime:\n"
        "  backend: cpu\n"
        "  batch_samples: 64\n"
        "  queue_samples: 256\n"
        "  prune_threshold_db: -60.0\n"
        "  prune_hysteresis_db: 3.0\n"
        "  prune_fold_to_awgn: true\n" +
        base_devices_and_models);
    const auto cfg = ocg::load_config_file(path);
    std::remove(path.c_str());
    require(cfg.runtime.prune_threshold_db.has_value(), "prune_threshold_db parsed");
    require(std::fabs(*cfg.runtime.prune_threshold_db - (-60.0)) < 1e-9, "prune_threshold_db value");
    require(std::fabs(cfg.runtime.prune_hysteresis_db - 3.0) < 1e-9, "prune_hysteresis_db value");
    require(cfg.runtime.prune_fold_to_awgn, "prune_fold_to_awgn value");
  }

  // 2. Explicit "off" leaves prune_threshold_db unset.
  {
    const std::string path = write_temp_yaml(
        "runtime:\n"
        "  backend: cpu\n"
        "  batch_samples: 64\n"
        "  queue_samples: 256\n"
        "  prune_threshold_db: off\n" +
        base_devices_and_models);
    const auto cfg = ocg::load_config_file(path);
    std::remove(path.c_str());
    require(!cfg.runtime.prune_threshold_db.has_value(), "prune_threshold_db=off leaves option unset");
  }

  // 3. Reject negative hysteresis.
  {
    const std::string path = write_temp_yaml(
        "runtime:\n"
        "  backend: cpu\n"
        "  batch_samples: 64\n"
        "  queue_samples: 256\n"
        "  prune_threshold_db: -60.0\n"
        "  prune_hysteresis_db: -1.0\n" +
        base_devices_and_models);
    bool threw = false;
    try {
      (void)ocg::load_config_file(path);
    } catch (const std::exception&) {
      threw = true;
    }
    std::remove(path.c_str());
    require(threw, "negative prune_hysteresis_db must be rejected");
  }

  // 4. Reject hysteresis without threshold.
  {
    const std::string path = write_temp_yaml(
        "runtime:\n"
        "  backend: cpu\n"
        "  batch_samples: 64\n"
        "  queue_samples: 256\n"
        "  prune_hysteresis_db: 2.0\n" +
        base_devices_and_models);
    bool threw = false;
    try {
      (void)ocg::load_config_file(path);
    } catch (const std::exception&) {
      threw = true;
    }
    std::remove(path.c_str());
    require(threw, "prune_hysteresis_db without prune_threshold_db must be rejected");
  }

  // 5. Reject prune_fold_to_awgn without explicit noise_power rx_model.
  {
    const std::string path = write_temp_yaml(
        "runtime:\n"
        "  backend: cpu\n"
        "  batch_samples: 64\n"
        "  queue_samples: 256\n"
        "  prune_threshold_db: -60.0\n"
        "  prune_fold_to_awgn: true\n"
        "devices:\n"
        "  - id: gnb0\n"
        "    sample_rate_hz: 23040000\n"
        "    tx_endpoint: tcp://127.0.0.1:4000\n"
        "    rx_endpoint: tcp://*:4001\n"
        "  - id: ue0\n"
        "    sample_rate_hz: 23040000\n"
        "    tx_endpoint: tcp://127.0.0.1:4002\n"
        "    rx_endpoint: tcp://*:4003\n"
        "links:\n"
        "  - from: gnb0\n"
        "    to: ue0\n"
        "    model: m0\n"
        "  - from: ue0\n"
        "    to: gnb0\n"
        "    model: m0\n"
        "models:\n"
        "  m0:\n"
        "    chain:\n"
        "      - type: tdl\n"
        "        taps:\n"
        "          - delay_samples: 0.0\n"
        "            gain_db: 0.0\n"
        "            phase_rad: 0.0\n");
    bool threw = false;
    try {
      (void)ocg::load_config_file(path);
    } catch (const std::exception&) {
      threw = true;
    }
    std::remove(path.c_str());
    require(threw, "prune_fold_to_awgn without explicit noise_power rx_model must be rejected");
  }
}

ocg::TopologyConfig make_multicell_1x1(ocg::Backend backend, std::size_t count,
                                       std::optional<double> threshold_db,
                                       double hysteresis_db = 0.0,
                                       bool fold_to_awgn = false,
                                       double rx_noise_power = -1.0)
{
  ocg::TopologyConfig cfg;
  cfg.runtime.backend = backend;
  cfg.runtime.batch_samples_auto = false;
  cfg.runtime.batch_samples = count;
  cfg.runtime.queue_samples = count * 4;
  cfg.runtime.prune_threshold_db = threshold_db;
  cfg.runtime.prune_hysteresis_db = hysteresis_db;
  cfg.runtime.prune_fold_to_awgn = fold_to_awgn;

  const std::string rx_id = rx_noise_power >= 0.0 ? "rx_noise" : "";
  cfg.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx0", .rx_endpoint = "rx0", .rx_model = rx_id},
      {.id = "gnb1", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx1", .rx_endpoint = "rx1", .rx_model = rx_id},
      {.id = "gnb2", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx2", .rx_endpoint = "rx2", .rx_model = rx_id},
      {.id = "gnb3", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx3", .rx_endpoint = "rx3", .rx_model = rx_id},
      {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000, .tx_endpoint = "tx4", .rx_endpoint = "rx4", .rx_model = rx_id},
  };
  cfg.links = {
      {.from = "gnb0", .to = "ue0", .model = "serving"},
      {.from = "gnb1", .to = "ue0", .model = "strong_if"},
      {.from = "gnb2", .to = "ue0", .model = "far_if"},
      {.from = "gnb3", .to = "ue0", .model = "far_if"},
      {.from = "ue0",  .to = "gnb0", .model = "serving"},
      {.from = "ue0",  .to = "gnb1", .model = "strong_if"},
      {.from = "ue0",  .to = "gnb2", .model = "far_if"},
      {.from = "ue0",  .to = "gnb3", .model = "far_if"},
  };
  cfg.models.emplace("serving", make_tdl_pathloss_model("serving", 6.0, 2.0));
  cfg.models.emplace("strong_if", make_tdl_pathloss_model("strong_if", 18.0, 3.0));
  cfg.models.emplace("far_if", make_tdl_pathloss_model("far_if", 85.0, 5.0));
  if (rx_noise_power >= 0.0) {
    cfg.models.emplace("rx_noise", make_rx_noise_model("rx_noise", rx_noise_power));
  }
  return cfg;
}

void test_static_1x1_pruning_and_negative_control()
{
  constexpr std::size_t kCount = 512;
  const auto s0 = make_signal(kCount, 0.11F, 0.0F);
  const auto s1 = make_signal(kCount, 0.23F, 0.7F);
  const auto s2 = make_signal(kCount, 0.37F, 1.3F);
  const auto s3 = make_signal(kCount, 0.53F, 2.1F);

  auto run_one = [&](ocg::Backend backend, std::optional<double> threshold_db,
                     ocg::ProcessorTimings& timings_out) {
    auto cfg = make_multicell_1x1(backend, kCount, threshold_db);
    auto proc = ocg::create_channel_processor(cfg);
    std::vector<ocg::SuperpositionInput> inputs = {
        {.link_key = "gnb0>ue0:serving",   .model = &cfg.models.at("serving"),   .samples = s0},
        {.link_key = "gnb1>ue0:strong_if", .model = &cfg.models.at("strong_if"), .samples = s1},
        {.link_key = "gnb2>ue0:far_if",    .model = &cfg.models.at("far_if"),    .samples = s2},
        {.link_key = "gnb3>ue0:far_if",    .model = &cfg.models.at("far_if"),    .samples = s3},
    };
    std::vector<ocg::IqSample> out(kCount);
    proc->process_superposition("ue0", inputs, nullptr, 23040000ULL,
                                std::span<ocg::IqSample>(out.data(), out.size()));
    timings_out = proc->last_timings();
    return out;
  };

  ocg::ProcessorTimings t_cpu_off, t_cpu_on, t_cpu_over;
  const auto cpu_off = run_one(ocg::Backend::Cpu, std::nullopt, t_cpu_off);
  const auto cpu_on = run_one(ocg::Backend::Cpu, -60.0, t_cpu_on);
  const auto cpu_over = run_one(ocg::Backend::Cpu, -10.0, t_cpu_over);

  require(t_cpu_off.active_lanes == 4 && t_cpu_off.total_lanes == 4, "CPU off runs all 4 lanes");
  require(t_cpu_on.active_lanes == 2 && t_cpu_on.total_lanes == 4, "CPU on prunes 2 far lanes");
  require(max_abs_diff(cpu_off, cpu_on) < 2e-4F,
          "pruning -85 dB interferers stays within 2e-4 of full superposition");

  // Negative control: threshold -10 dB also prunes the -18 dB strong interferer (~0.126 amplitude).
  require(t_cpu_over.active_lanes == 1 && t_cpu_over.total_lanes == 4,
          "negative control prunes strong interferer too");
  require(max_abs_diff(cpu_off, cpu_over) > 5e-2F,
          "negative control must visibly alter output when strong interferer is pruned");

  // All-edges-pruned case: output is identically zero without rx_model.
  ocg::ProcessorTimings t_cpu_zero;
  const auto cpu_zero = run_one(ocg::Backend::Cpu, 20.0, t_cpu_zero);
  require(t_cpu_zero.active_lanes == 0 && t_cpu_zero.total_lanes == 4, "all lanes pruned at +20 dB");
  require(avg_power(cpu_zero) == 0.0, "all-pruned output without rx_model must be zero");

  if (has_cuda_gpu()) {
    ocg::ProcessorTimings t_gpu_off, t_gpu_on, t_gpu_over, t_gpu_zero;
    const auto gpu_off = run_one(ocg::Backend::Cuda, std::nullopt, t_gpu_off);
    const auto gpu_on = run_one(ocg::Backend::Cuda, -60.0, t_gpu_on);
    const auto gpu_over = run_one(ocg::Backend::Cuda, -10.0, t_gpu_over);
    const auto gpu_zero = run_one(ocg::Backend::Cuda, 20.0, t_gpu_zero);

    require(t_gpu_off.used_device_channel, "CUDA off uses device channel");
    require(t_gpu_on.used_device_channel, "CUDA on uses device channel");
    require(t_gpu_off.active_lanes == 4 && t_gpu_off.total_lanes == 4, "CUDA off runs all 4 lanes");
    require(t_gpu_on.active_lanes == 2 && t_gpu_on.total_lanes == 4, "CUDA on prunes 2 far lanes");
    require(t_gpu_over.active_lanes == 1 && t_gpu_over.total_lanes == 4, "CUDA negative control active_lanes");
    require(t_gpu_zero.active_lanes == 0 && t_gpu_zero.total_lanes == 4, "CUDA all-pruned active_lanes");
    require(max_abs_diff(cpu_off, gpu_off) < 1e-3F, "CPU/CUDA parity off");
    require(max_abs_diff(cpu_on, gpu_on) < 1e-3F, "CPU/CUDA parity on");
    require(max_abs_diff(cpu_over, gpu_over) < 1e-3F, "CPU/CUDA parity negative control");
    require(avg_power(gpu_zero) == 0.0, "CUDA all-pruned output without rx_model must be zero");
  }
}

void test_mimo_2x2_pruning_parity()
{
  constexpr std::size_t kCount = 256;
  const auto tx00 = make_signal(kCount, 0.13F, 0.0F);
  const auto tx01 = make_signal(kCount, 0.17F, 0.5F);
  const auto tx10 = make_signal(kCount, 0.29F, 1.1F);
  const auto tx11 = make_signal(kCount, 0.31F, 1.9F);

  auto make_mimo_cfg = [&](ocg::Backend backend, std::optional<double> threshold_db) {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = backend;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = kCount;
    cfg.runtime.queue_samples = kCount * 4;
    cfg.runtime.prune_threshold_db = threshold_db;
    cfg.devices = {
        {.id = "g0_p0", .sample_rate_hz = 23040000, .tx_endpoint = "t0", .rx_endpoint = "r0"},
        {.id = "g0_p1", .sample_rate_hz = 23040000, .tx_endpoint = "t1", .rx_endpoint = "r1"},
        {.id = "g1_p0", .sample_rate_hz = 23040000, .tx_endpoint = "t2", .rx_endpoint = "r2"},
        {.id = "g1_p1", .sample_rate_hz = 23040000, .tx_endpoint = "t3", .rx_endpoint = "r3"},
        {.id = "u0_p0", .sample_rate_hz = 23040000, .tx_endpoint = "t4", .rx_endpoint = "r4"},
        {.id = "u0_p1", .sample_rate_hz = 23040000, .tx_endpoint = "t5", .rx_endpoint = "r5"},
    };
    cfg.radio_nodes = {
        {.id = "gnb0", .tx_ports = {"g0_p0", "g0_p1"}, .rx_ports = {"g0_p0", "g0_p1"}},
        {.id = "gnb1", .tx_ports = {"g1_p0", "g1_p1"}, .rx_ports = {"g1_p0", "g1_p1"}},
        {.id = "ue0",  .tx_ports = {"u0_p0", "u0_p1"}, .rx_ports = {"u0_p0", "u0_p1"}},
    };
    cfg.links = {
        {.from = "gnb0", .to = "ue0", .model = "serving"},
        {.from = "gnb1", .to = "ue0", .model = "far_if"},
        {.from = "ue0",  .to = "gnb0", .model = "serving"},
        {.from = "ue0",  .to = "gnb1", .model = "far_if"},
    };
    cfg.models.emplace("serving", make_tdl_pathloss_model("serving", 6.0, 1.0));
    cfg.models.emplace("far_if", make_tdl_pathloss_model("far_if", 85.0, 2.0));
    return cfg;
  };

  auto run_mimo = [&](ocg::Backend backend, std::optional<double> threshold_db,
                      ocg::ProcessorTimings& timings_out) {
    auto cfg = make_mimo_cfg(backend, threshold_db);
    auto proc = ocg::create_channel_processor(cfg);
    const auto resolved = ocg::resolve_topology(cfg);
    std::vector<ocg::SuperpositionInput> inputs;
    for (const auto& lane : resolved.lanes) {
      if (lane.dst_node != "ue0") continue;
      const auto* sig = &tx00;
      if (lane.src_device == "g0_p1") sig = &tx01;
      else if (lane.src_device == "g1_p0") sig = &tx10;
      else if (lane.src_device == "g1_p1") sig = &tx11;
      inputs.push_back({.link_key = lane.key,
                        .model = &cfg.models.at(lane.model_id),
                        .samples = *sig,
                        .rx_port = lane.rx_port,
                        .tx_port = lane.tx_port});
    }
    std::vector<ocg::IqSample> row0(kCount), row1(kCount);
    std::span<ocg::IqSample> rows[2] = {row0, row1};
    proc->process_superposition("ue0", inputs, nullptr, 23040000ULL,
                                std::span<std::span<ocg::IqSample>>(rows));
    timings_out = proc->last_timings();
    return std::make_pair(row0, row1);
  };

  ocg::ProcessorTimings t_cpu_off, t_cpu_on;
  const auto [cpu_off_r0, cpu_off_r1] = run_mimo(ocg::Backend::Cpu, std::nullopt, t_cpu_off);
  const auto [cpu_on_r0, cpu_on_r1] = run_mimo(ocg::Backend::Cpu, -60.0, t_cpu_on);
  require(t_cpu_on.active_lanes == 4 && t_cpu_on.total_lanes == 8,
          "MIMO 2x2 CPU prunes all 4 lanes of far_if");
  require(max_abs_diff(cpu_off_r0, cpu_on_r0) < 2e-4F, "MIMO 2x2 row0 accuracy");
  require(max_abs_diff(cpu_off_r1, cpu_on_r1) < 2e-4F, "MIMO 2x2 row1 accuracy");

  if (has_cuda_gpu()) {
    ocg::ProcessorTimings t_gpu_on;
    const auto [gpu_on_r0, gpu_on_r1] = run_mimo(ocg::Backend::Cuda, -60.0, t_gpu_on);
    require(t_gpu_on.active_lanes == 4 && t_gpu_on.total_lanes == 8,
            "MIMO 2x2 CUDA prunes all 4 lanes of far_if");
    require(max_abs_diff(cpu_on_r0, gpu_on_r0) < 1e-3F, "MIMO 2x2 row0 CPU/CUDA parity");
    require(max_abs_diff(cpu_on_r1, gpu_on_r1) < 1e-3F, "MIMO 2x2 row1 CPU/CUDA parity");
  }
}

void test_dynamic_hysteresis_and_reactivation()
{
  constexpr std::size_t kCount = 256;
  const auto s0 = make_signal(kCount, 0.10F, 0.0F);
  const auto s1 = make_signal(kCount, 0.27F, 0.4F);

  auto make_dyn_cfg = [&](ocg::Backend backend) {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = backend;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = kCount;
    cfg.runtime.queue_samples = kCount * 4;
    cfg.runtime.prune_threshold_db = -60.0;
    cfg.runtime.prune_hysteresis_db = 5.0; // re-activate requires >= -55 dB
    cfg.devices = {
        {.id = "gnb0", .sample_rate_hz = 23040000, .tx_endpoint = "t0", .rx_endpoint = "r0"},
        {.id = "gnb1", .sample_rate_hz = 23040000, .tx_endpoint = "t1", .rx_endpoint = "r1"},
        {.id = "ue0",  .sample_rate_hz = 23040000, .tx_endpoint = "t2", .rx_endpoint = "r2"},
    };
    cfg.links = {
        {.from = "gnb0", .to = "ue0", .model = "serving"},
        {.from = "gnb1", .to = "ue0", .model = "dyn_if"},
        {.from = "ue0",  .to = "gnb0", .model = "serving"},
        {.from = "ue0",  .to = "gnb1", .model = "dyn_if"},
    };
    cfg.models.emplace("serving", make_tdl_pathloss_model("serving", 6.0, 2.0));
    cfg.models.emplace("dyn_if", make_tdl_pathloss_model("dyn_if", 70.0, 4.0));
    return cfg;
  };

  auto run_sequence = [&](ocg::Backend backend) {
    auto cfg = make_dyn_cfg(backend);
    auto proc = ocg::create_channel_processor(cfg);
    auto ctl_map = proc->collect_control_links();
    ocg::BrokerLinkControl* ctl = ctl_map.at("gnb1>ue0:dyn_if");
    std::vector<ocg::SuperpositionInput> inputs = {
        {.link_key = "gnb0>ue0:serving", .model = &cfg.models.at("serving"), .samples = s0},
        {.link_key = "gnb1>ue0:dyn_if",  .model = &cfg.models.at("dyn_if"),  .samples = s1},
    };
    auto set_pl = [&](float pl_db) {
      ctl->shadow.path_loss_db = pl_db;
      ctl->seqno.fetch_add(1, std::memory_order_release);
    };
    auto step = [&]() {
      std::vector<ocg::IqSample> out(kCount);
      proc->process_superposition("ue0", inputs, nullptr, 23040000ULL,
                                  std::span<ocg::IqSample>(out.data(), out.size()));
      return std::make_pair(out, proc->last_timings());
    };

    std::vector<std::vector<ocg::IqSample>> outputs;
    // Slot 0: -70 dB -> pruned (1 active).
    auto [o0, t0] = step();
    require(t0.active_lanes == 1, "slot 0: -70 dB pruned");
    outputs.push_back(std::move(o0));

    // Slot 1: -58 dB -> inside hysteresis band [-60, -55), stays pruned.
    set_pl(58.0F);
    auto [o1, t1] = step();
    require(t1.active_lanes == 1, "slot 1: -58 dB stays pruned due to hysteresis");
    outputs.push_back(std::move(o1));

    // Slot 2: -12 dB -> above -55 dB, re-activates cleanly.
    set_pl(12.0F);
    auto [o2, t2] = step();
    require(t2.active_lanes == 2, "slot 2: -12 dB re-activates");
    outputs.push_back(std::move(o2));

    // Slot 3: -58 dB -> now active, above -60 dB threshold, stays active.
    set_pl(58.0F);
    auto [o3, t3] = step();
    require(t3.active_lanes == 2, "slot 3: -58 dB stays active due to hysteresis");
    outputs.push_back(std::move(o3));

    // Slot 4: -65 dB -> below -60 dB, pruned again.
    set_pl(65.0F);
    auto [o4, t4] = step();
    require(t4.active_lanes == 1, "slot 4: -65 dB pruned again");
    outputs.push_back(std::move(o4));

    // Slot 5: profile_swap to strong 0 dB tap (still path_loss 65 -> -65 dB) then +15 dB tap -> -50 dB active.
    {
      ctl->shadow_profile.n_taps = 1;
      ctl->shadow_profile.taps[0] = {.delay_samples = 2.0, .gain_db = 15.0, .phase_rad = 0.2};
      ctl->profile_pending = true;
      ctl->seqno.fetch_add(1, std::memory_order_release);
    }
    auto [o5, t5] = step();
    require(t5.active_lanes == 2, "slot 5: profile_swap raises total gain to -50 dB and re-activates");
    outputs.push_back(std::move(o5));

    return outputs;
  };

  const auto cpu_seq = run_sequence(ocg::Backend::Cpu);
  if (has_cuda_gpu()) {
    const auto gpu_seq = run_sequence(ocg::Backend::Cuda);
    require(cpu_seq.size() == gpu_seq.size(), "same sequence length");
    for (std::size_t i = 0; i != cpu_seq.size(); ++i) {
      require(max_abs_diff(cpu_seq[i], gpu_seq[i]) < 1e-3F,
              "dynamic hysteresis/reactivation CPU/CUDA parity");
    }
  }
}

void test_residual_awgn_folding()
{
  constexpr std::size_t kCount = 8192;
  constexpr int kWeakCells = 16;
  constexpr double kBaseNoise = 1.0e-4;
  constexpr double kWeakPathLossDb = 30.0; // each cell contributes 1.0 * 1e-3 = 1e-3 -> 16 cells = 0.016

  auto run_fold = [&](ocg::Backend backend, bool fold, bool silent_interferers,
                      ocg::ProcessorTimings& timings_out) {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = backend;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = kCount;
    cfg.runtime.queue_samples = kCount * 4;
    cfg.runtime.prune_threshold_db = -20.0; // prunes all -30 dB cells
    cfg.runtime.prune_fold_to_awgn = fold;

    cfg.devices.push_back({.id = "ue0", .sample_rate_hz = 23040000,
                           .tx_endpoint = "t0", .rx_endpoint = "r0", .rx_model = "rx_noise"});
    for (int c = 0; c < kWeakCells; ++c) {
      const std::string gid = "gnb" + std::to_string(c);
      cfg.devices.push_back({.id = gid, .sample_rate_hz = 23040000,
                             .tx_endpoint = "tg" + std::to_string(c),
                             .rx_endpoint = "rg" + std::to_string(c),
                             .rx_model = "rx_noise"});
      cfg.links.push_back({.from = gid, .to = "ue0", .model = "weak_if"});
      cfg.links.push_back({.from = "ue0", .to = gid, .model = "weak_if"});
    }
    cfg.models.emplace("weak_if", make_tdl_pathloss_model("weak_if", kWeakPathLossDb, 0.0));
    cfg.models.emplace("rx_noise", make_rx_noise_model("rx_noise", kBaseNoise));

    auto proc = ocg::create_channel_processor(cfg);
    std::vector<std::vector<ocg::IqSample>> sigs(kWeakCells);
    std::vector<ocg::SuperpositionInput> inputs;
    for (int c = 0; c < kWeakCells; ++c) {
      sigs[c] = silent_interferers
                    ? std::vector<ocg::IqSample>(kCount, {0.0F, 0.0F})
                    : make_signal(kCount, 0.07F * static_cast<float>(c + 1), 0.1F * static_cast<float>(c));
      inputs.push_back({.link_key = "gnb" + std::to_string(c) + ">ue0:weak_if",
                        .model = &cfg.models.at("weak_if"),
                        .samples = sigs[c]});
    }
    std::vector<ocg::IqSample> out(kCount);
    proc->process_superposition("ue0", inputs, &cfg.models.at("rx_noise"), 23040000ULL,
                                std::span<ocg::IqSample>(out.data(), out.size()));
    timings_out = proc->last_timings();
    return avg_power(out);
  };

  // Analytic expectation: unit-amplitude complex sinusoids -> P_tx = 1.0 each,
  // 16 cells * 1e-3 = 0.016 folded noise power.
  const double expected_folded = static_cast<double>(kWeakCells) * std::pow(10.0, -kWeakPathLossDb / 10.0);

  ocg::ProcessorTimings t_cpu_nofold, t_cpu_fold, t_cpu_silent;
  const double p_cpu_nofold = run_fold(ocg::Backend::Cpu, false, false, t_cpu_nofold);
  const double p_cpu_fold = run_fold(ocg::Backend::Cpu, true, false, t_cpu_fold);
  const double p_cpu_silent = run_fold(ocg::Backend::Cpu, true, true, t_cpu_silent);

  require(t_cpu_nofold.active_lanes == 0 && t_cpu_nofold.folded_noise_power == 0.0,
          "CPU nofold reports 0 folded noise");
  require(std::fabs(p_cpu_nofold - kBaseNoise) / kBaseNoise < 0.10,
          "CPU nofold output power matches thermal floor only");
  require(std::fabs(t_cpu_fold.folded_noise_power - expected_folded) / expected_folded < 1e-5,
          "CPU fold reports exact analytic residual interference power");
  require(std::fabs(p_cpu_fold - (kBaseNoise + expected_folded)) / (kBaseNoise + expected_folded) < 0.10,
          "CPU fold output variance matches thermal + aggregate pruned interference");
  require(t_cpu_silent.folded_noise_power == 0.0, "silent pruned interferers fold 0 noise");
  require(std::fabs(p_cpu_silent - kBaseNoise) / kBaseNoise < 0.10,
          "silent pruned interferers preserve thermal floor");

  if (has_cuda_gpu()) {
    ocg::ProcessorTimings t_gpu_nofold, t_gpu_fold, t_gpu_silent;
    const double p_gpu_nofold = run_fold(ocg::Backend::Cuda, false, false, t_gpu_nofold);
    const double p_gpu_fold = run_fold(ocg::Backend::Cuda, true, false, t_gpu_fold);
    const double p_gpu_silent = run_fold(ocg::Backend::Cuda, true, true, t_gpu_silent);

    require(t_gpu_fold.active_lanes == 0 && t_gpu_fold.total_lanes == kWeakCells,
            "CUDA fold active/total lane accounting");
    require(std::fabs(t_gpu_fold.folded_noise_power - t_cpu_fold.folded_noise_power) < 1e-9,
            "CPU/CUDA folded_noise_power bit-equivalent");
    require(std::fabs(p_gpu_nofold - kBaseNoise) / kBaseNoise < 0.10,
            "CUDA nofold output power matches thermal floor only");
    require(std::fabs(p_gpu_fold - (kBaseNoise + expected_folded)) / (kBaseNoise + expected_folded) < 0.10,
            "CUDA fold output variance matches thermal + aggregate pruned interference");
    require(t_gpu_silent.folded_noise_power == 0.0, "CUDA silent pruned interferers fold 0 noise");
    require(std::fabs(p_gpu_silent - kBaseNoise) / kBaseNoise < 0.10,
            "CUDA silent pruned interferers preserve thermal floor");
  }
}

} // namespace

int main()
{
  test_config_parsing_and_validation();
  test_static_1x1_pruning_and_negative_control();
  test_mimo_2x2_pruning_parity();
  test_dynamic_hysteresis_and_reactivation();
  test_residual_awgn_folding();
  std::cout << "test_interference_pruning passed\n";
  return 0;
}
