// Unit, concurrency, config, and bit-exact CUDA parity tests for
// multithreaded CUDA host preparation (`runtime.cuda_prep_threads`).

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/prep_executor.h"
#include "ocudu_gpu_channel/processing.h"

#include <atomic>
#include <barrier>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
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
    throw std::runtime_error(message);
  }
}

bool bit_equal(const std::vector<ocg::IqSample>& a, const std::vector<ocg::IqSample>& b)
{
  return a.size() == b.size() &&
         std::memcmp(a.data(), b.data(), a.size() * sizeof(ocg::IqSample)) == 0;
}

std::vector<ocg::IqSample> make_signal(std::size_t count, float freq, float phase)
{
  std::vector<ocg::IqSample> out(count);
  for (std::size_t i = 0; i != count; ++i) {
    const float angle = freq * static_cast<float>(i) + phase;
    out[i] = {0.5F * std::cos(angle), 0.5F * std::sin(angle)};
  }
  return out;
}

void test_prep_executor()
{
  // Reject zero threads.
  bool rejected_zero = false;
  try {
    ocg::PrepExecutor invalid(0);
  } catch (const std::invalid_argument&) {
    rejected_zero = true;
  }
  require(rejected_zero, "PrepExecutor must reject zero threads");

  // Single-thread mode runs inline.
  {
    ocg::PrepExecutor single(1);
    require(single.thread_count() == 1, "single thread_count");
    std::vector<int> hits(17, 0);
    single.parallel_for(hits.size(), [&](std::size_t i) { ++hits[i]; });
    for (int h : hits) {
      require(h == 1, "single-thread coverage");
    }
    single.parallel_for(0, [](std::size_t) { require(false, "empty count must not run"); });
  }

  // Concurrent barrier proves caller + (N-1) workers execute together.
  {
    constexpr std::size_t kThreads = 8;
    ocg::PrepExecutor pool(kThreads);
    require(pool.thread_count() == kThreads, "pool thread_count");
    std::barrier ready(static_cast<std::ptrdiff_t>(kThreads));
    std::mutex mu;
    std::set<std::thread::id> ids;
    pool.parallel_for(kThreads, [&](std::size_t) {
      {
        std::lock_guard<std::mutex> lock(mu);
        ids.insert(std::this_thread::get_id());
      }
      ready.arrive_and_wait();
    });
    require(ids.size() == kThreads, "all 8 threads (caller + 7 workers) must run concurrently");

    // Uneven count coverage.
    std::vector<int> hits(137, 0);
    pool.parallel_for(hits.size(), [&](std::size_t i) { ++hits[i]; });
    for (int h : hits) {
      require(h == 1, "uneven parallel_for must cover every index once");
    }

    // Exception propagation after joining all tasks.
    std::atomic<int> completed{0};
    bool threw = false;
    try {
      pool.parallel_for(32, [&](std::size_t i) {
        completed.fetch_add(1, std::memory_order_relaxed);
        if (i == 13) {
          throw std::runtime_error("injected prep error");
        }
      });
    } catch (const std::runtime_error&) {
      threw = true;
    }
    require(threw && completed.load() == 32, "must join all tasks and rethrow worker error");

    // Multi-caller concurrent stress test (simulating concurrent broker RX nodes).
    constexpr int kCallers = 8;
    constexpr int kIters = 200;
    std::vector<std::thread> callers;
    std::atomic<bool> failed{false};
    for (int c = 0; c < kCallers; ++c) {
      callers.emplace_back([&, c] {
        for (int it = 0; it < kIters; ++it) {
          const std::size_t n = 16 + static_cast<std::size_t>((c + it) % 19);
          std::vector<int> local(n, 0);
          pool.parallel_for(n, [&](std::size_t i) { local[i] = static_cast<int>(i + 1); });
          for (std::size_t i = 0; i < n; ++i) {
            if (local[i] != static_cast<int>(i + 1)) {
              failed.store(true, std::memory_order_release);
              return;
            }
          }
        }
      });
    }
    for (auto& t : callers) {
      t.join();
    }
    require(!failed.load(), "concurrent multi-caller PrepExecutor stress must be exact");
  }
}

void test_config_parsing()
{
  const std::string path =
      "/tmp/ocg_test_cuda_prep_" + std::to_string(getpid()) + ".yaml";
  {
    std::ofstream out(path);
    out << R"(
runtime:
  backend: cuda
  cuda_prep_threads: 8
  batch_samples: 1024
  queue_samples: 4096
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:4100
    rx_endpoint: tcp://*:4101
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:4102
    rx_endpoint: tcp://*:4103
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
      - type: path_loss
        path_loss_db: 6.0
)";
  }
  const auto cfg = ocg::load_config_file(path);
  std::remove(path.c_str());
  require(cfg.runtime.cuda_prep_threads == 8, "cuda_prep_threads parses");
  require(ocg::validate_config(cfg).empty(), "valid cuda_prep_threads passes validation");

  auto invalid = cfg;
  invalid.runtime.cuda_prep_threads = 0;
  const auto errs = ocg::validate_config(invalid);
  require(!errs.empty(), "cuda_prep_threads == 0 must fail validation");
}

#if OCUDU_GPU_CHANNEL_HAS_CUDA
bool has_cuda_gpu()
{
  return ocg::cuda_runtime_probe();
}

ocg::TopologyConfig build_fanin_cfg(std::size_t n_ues,
                                    std::size_t batch,
                                    std::size_t prep_threads,
                                    bool leading_tdl,
                                    bool snr_awgn,
                                    bool explicit_awgn)
{
  ocg::TopologyConfig cfg;
  cfg.runtime.backend = ocg::Backend::Cuda;
  cfg.runtime.gpu_device = 0;
  cfg.runtime.cuda_host_memory = ocg::CudaHostMemory::Copy;
  cfg.runtime.cuda_prep_threads = prep_threads;
  cfg.runtime.batch_samples_auto = false;
  cfg.runtime.batch_samples = batch;
  cfg.runtime.queue_samples = std::max<std::size_t>(batch * 4, 4096);

  cfg.devices.push_back({.id = "gnb0",
                         .role = "gnb",
                         .sample_rate_hz = 23040000,
                         .tx_endpoint = "tcp://127.0.0.1:5000",
                         .rx_endpoint = "tcp://*:5001",
                         .rx_model = explicit_awgn ? "rx_noise" : ""});
  for (std::size_t i = 0; i < n_ues; ++i) {
    cfg.devices.push_back({.id = "ue" + std::to_string(i),
                           .role = "ue",
                           .sample_rate_hz = 23040000,
                           .tx_endpoint = "tcp://127.0.0.1:" + std::to_string(5002 + 2 * i),
                           .rx_endpoint = "tcp://*:" + std::to_string(5003 + 2 * i),
                           .rx_model = explicit_awgn ? "rx_noise" : ""});
    cfg.links.push_back({"gnb0", "ue" + std::to_string(i), "m0"});
    cfg.links.push_back({"ue" + std::to_string(i), "gnb0", "m0"});
  }

  ocg::ModelConfig m0;
  m0.id = "m0";
  if (leading_tdl) {
    ocg::ModelStep tdl;
    tdl.type = ocg::ModelStepType::Tdl;
    tdl.taps_declared = true;
    tdl.taps = {{0.0, 0.0, 0.0}, {1.5, -3.5, 0.4}, {4.2, -7.0, -0.9}};
    m0.chain.push_back(std::move(tdl));
  }
  m0.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 8.0}}});
  m0.chain.push_back({.type = ocg::ModelStepType::Cfo, .params = {{"cfo_hz", 150.0}}});
  if (snr_awgn) {
    m0.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"snr_db", 22.0}}});
  }
  cfg.models.emplace("m0", std::move(m0));

  if (explicit_awgn) {
    ocg::ModelConfig rx;
    rx.id = "rx_noise";
    rx.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"noise_power", 1e-5}}});
    cfg.models.emplace("rx_noise", std::move(rx));
  }
  return cfg;
}

void verify_fanin_bit_exact(std::size_t n_ues,
                            std::size_t batch,
                            bool leading_tdl,
                            bool snr_awgn,
                            bool explicit_awgn,
                            const char* label)
{
  const auto cfg1 = build_fanin_cfg(n_ues, batch, 1, leading_tdl, snr_awgn, explicit_awgn);
  const auto cfg8 = build_fanin_cfg(n_ues, batch, 8, leading_tdl, snr_awgn, explicit_awgn);
  auto p1 = ocg::create_channel_processor(cfg1);
  auto p8 = ocg::create_channel_processor(cfg8);

  std::vector<std::vector<ocg::IqSample>> txs;
  txs.reserve(n_ues);
  for (std::size_t i = 0; i < n_ues; ++i) {
    txs.push_back(make_signal(batch, 0.011F + 0.002F * static_cast<float>(i), 0.2F * static_cast<float>(i)));
  }

  std::vector<ocg::SuperpositionInput> in1, in8;
  for (std::size_t i = 0; i < n_ues; ++i) {
    const std::string key = "ue" + std::to_string(i) + ">gnb0:m0";
    in1.push_back({key, &cfg1.models.at("m0"), txs[i], 0, 0});
    in8.push_back({key, &cfg8.models.at("m0"), txs[i], 0, 0});
  }
  const auto* rx1 = explicit_awgn ? &cfg1.models.at("rx_noise") : nullptr;
  const auto* rx8 = explicit_awgn ? &cfg8.models.at("rx_noise") : nullptr;

  std::vector<ocg::IqSample> out1(batch), out8(batch);
  for (int slot = 0; slot < 3; ++slot) {
    p1->process_superposition("gnb0", in1, rx1, 23040000ULL, out1);
    p8->process_superposition("gnb0", in8, rx8, 23040000ULL, out8);
    if (!bit_equal(out1, out8)) {
      throw std::runtime_error(std::string("bit-exact mismatch in ") + label);
    }
  }
}

void test_mimo_4x4_parallel_copy_and_out()
{
  // 4x4 MIMO at 61,440 samples -> 4 * 491 KB = 1.96 MB for both input and
  // output copy, exercising sub-source chunking and parallel host_out copy.
  constexpr std::size_t kBatch = 61440;
  auto make_cfg = [](std::size_t prep_threads) {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = ocg::Backend::Cuda;
    cfg.runtime.gpu_device = 0;
    cfg.runtime.cuda_host_memory = ocg::CudaHostMemory::Copy;
    cfg.runtime.cuda_prep_threads = prep_threads;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = kBatch;
    cfg.runtime.queue_samples = kBatch * 4;
    ocg::RadioNodeConfig gnode{.id = "gnb0"};
    ocg::RadioNodeConfig unode{.id = "ue0"};
    int port = 6000;
    for (int a = 0; a < 4; ++a) {
      const std::string gd = "gnb0_p" + std::to_string(a);
      const std::string ud = "ue0_p" + std::to_string(a);
      cfg.devices.push_back({.id = gd, .role = "gnb", .sample_rate_hz = 122880000,
                             .tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++),
                             .rx_endpoint = "tcp://*:" + std::to_string(port++)});
      cfg.devices.push_back({.id = ud, .role = "ue", .sample_rate_hz = 122880000,
                             .tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++),
                             .rx_endpoint = "tcp://*:" + std::to_string(port++)});
      gnode.tx_ports.push_back(gd);
      gnode.rx_ports.push_back(gd);
      unode.tx_ports.push_back(ud);
      unode.rx_ports.push_back(ud);
    }
    cfg.radio_nodes = {gnode, unode};
    cfg.links = {{"gnb0", "ue0", "m0"}, {"ue0", "gnb0", "m0"}};
    ocg::ModelConfig m0;
    m0.id = "m0";
    ocg::ModelStep tdl;
    tdl.type = ocg::ModelStepType::Tdl;
    tdl.taps_declared = true;
    tdl.taps = {{0.0, 0.0, 0.0}, {2.0, -4.0, 0.5}};
    m0.chain.push_back(std::move(tdl));
    m0.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    cfg.models.emplace("m0", std::move(m0));
    return cfg;
  };

  const auto cfg1 = make_cfg(1);
  const auto cfg8 = make_cfg(8);
  auto p1 = ocg::create_channel_processor(cfg1);
  auto p8 = ocg::create_channel_processor(cfg8);
  const auto res1 = ocg::resolve_topology(cfg1);
  const auto res8 = ocg::resolve_topology(cfg8);

  std::vector<std::vector<ocg::IqSample>> txs(4);
  for (int a = 0; a < 4; ++a) {
    txs[static_cast<std::size_t>(a)] = make_signal(kBatch, 0.015F + 0.003F * a, 0.4F * a);
  }
  std::vector<ocg::SuperpositionInput> in1, in8;
  for (const auto& lane : res1.lanes) {
    if (lane.dst_node != "ue0") continue;
    in1.push_back({lane.key, &cfg1.models.at(lane.model_id),
                   txs[static_cast<std::size_t>(lane.tx_port)], lane.rx_port, lane.tx_port});
  }
  for (const auto& lane : res8.lanes) {
    if (lane.dst_node != "ue0") continue;
    in8.push_back({lane.key, &cfg8.models.at(lane.model_id),
                   txs[static_cast<std::size_t>(lane.tx_port)], lane.rx_port, lane.tx_port});
  }

  std::vector<std::vector<ocg::IqSample>> out1(4, std::vector<ocg::IqSample>(kBatch));
  std::vector<std::vector<ocg::IqSample>> out8(4, std::vector<ocg::IqSample>(kBatch));
  std::span<ocg::IqSample> r1[4] = {out1[0], out1[1], out1[2], out1[3]};
  std::span<ocg::IqSample> r8[4] = {out8[0], out8[1], out8[2], out8[3]};
  for (int slot = 0; slot < 2; ++slot) {
    p1->process_superposition("ue0", in1, nullptr, 122880000ULL, r1);
    p8->process_superposition("ue0", in8, nullptr, 122880000ULL, r8);
    for (int a = 0; a < 4; ++a) {
      require(bit_equal(out1[static_cast<std::size_t>(a)], out8[static_cast<std::size_t>(a)]),
              "4x4 MIMO parallel sub-source copy & host_out must be bit-exact");
    }
  }
}

void test_concurrent_multi_rx_nodes()
{
  // 4 distinct RX nodes (2 gNBs + 2 UEs, 8 sources/node at 23,040 samples ->
  // > 1.5 MiB for gNBs, exercising concurrent multi-caller PrepExecutor on GPU).
  constexpr std::size_t kBatch = 23040;
  constexpr std::size_t kUes = 10;
  auto make_cfg = [](std::size_t prep_threads) {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = ocg::Backend::Cuda;
    cfg.runtime.gpu_device = 0;
    cfg.runtime.cuda_host_memory = ocg::CudaHostMemory::Copy;
    cfg.runtime.cuda_prep_threads = prep_threads;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = kBatch;
    cfg.runtime.queue_samples = kBatch * 4;
    int port = 7000;
    for (int g = 0; g < 2; ++g) {
      cfg.devices.push_back({.id = "gnb" + std::to_string(g), .role = "gnb", .sample_rate_hz = 23040000,
                             .tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++),
                             .rx_endpoint = "tcp://*:" + std::to_string(port++)});
    }
    for (std::size_t u = 0; u < kUes; ++u) {
      cfg.devices.push_back({.id = "ue" + std::to_string(u), .role = "ue", .sample_rate_hz = 23040000,
                             .tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++),
                             .rx_endpoint = "tcp://*:" + std::to_string(port++)});
      for (int g = 0; g < 2; ++g) {
        cfg.links.push_back({"gnb" + std::to_string(g), "ue" + std::to_string(u), "m0"});
        cfg.links.push_back({"ue" + std::to_string(u), "gnb" + std::to_string(g), "m0"});
      }
    }
    ocg::ModelConfig m0;
    m0.id = "m0";
    ocg::ModelStep tdl;
    tdl.type = ocg::ModelStepType::Tdl;
    tdl.taps_declared = true;
    tdl.taps = {{0.0, 0.0, 0.0}, {1.2, -2.5, 0.3}};
    m0.chain.push_back(std::move(tdl));
    m0.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    m0.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"snr_db", 25.0}}});
    cfg.models.emplace("m0", std::move(m0));
    return cfg;
  };

  const auto cfg1 = make_cfg(1);
  const auto cfg8 = make_cfg(8);
  auto p1 = ocg::create_channel_processor(cfg1);
  auto p8 = ocg::create_channel_processor(cfg8);

  std::unordered_map<std::string, std::vector<ocg::IqSample>> txs;
  for (std::size_t i = 0; i < cfg1.devices.size(); ++i) {
    txs[cfg1.devices[i].id] = make_signal(kBatch, 0.012F + 0.0017F * i, 0.11F * i);
  }

  const std::vector<std::string> targets = {"gnb0", "gnb1", "ue0", "ue1"};
  auto build_inputs = [&](const ocg::TopologyConfig& cfg, const std::string& dst) {
    std::vector<ocg::SuperpositionInput> ins;
    for (const auto& lk : cfg.links) {
      if (lk.to != dst) continue;
      ins.push_back({lk.from + ">" + lk.to + ":" + lk.model, &cfg.models.at(lk.model),
                     txs.at(lk.from), 0, 0});
    }
    return ins;
  };

  // Reference serial outputs over 10 slots per node.
  constexpr int kSlots = 10;
  std::vector<std::vector<ocg::IqSample>> ref_final(targets.size(), std::vector<ocg::IqSample>(kBatch));
  for (std::size_t t = 0; t < targets.size(); ++t) {
    auto ins = build_inputs(cfg1, targets[t]);
    for (int s = 0; s < kSlots; ++s) {
      p1->process_superposition(targets[t], ins, nullptr, 23040000ULL, ref_final[t]);
    }
  }

  // Concurrent execution of all 4 RX nodes through the shared 8-thread PrepExecutor.
  std::vector<std::vector<ocg::IqSample>> par_final(targets.size(), std::vector<ocg::IqSample>(kBatch));
  std::vector<std::thread> threads;
  std::atomic<bool> failed{false};
  for (std::size_t t = 0; t < targets.size(); ++t) {
    threads.emplace_back([&, t] {
      try {
        auto ins = build_inputs(cfg8, targets[t]);
        for (int s = 0; s < kSlots; ++s) {
          p8->process_superposition(targets[t], ins, nullptr, 23040000ULL, par_final[t]);
        }
      } catch (...) {
        failed.store(true, std::memory_order_release);
      }
    });
  }
  for (auto& th : threads) {
    th.join();
  }
  require(!failed.load(), "concurrent multi-RX-node CUDA prep threw");
  for (std::size_t t = 0; t < targets.size(); ++t) {
    require(bit_equal(ref_final[t], par_final[t]),
            "concurrent multi-RX-node CUDA prep must match serial bit-for-bit");
  }
}
#endif

} // namespace

int main()
{
  test_prep_executor();
  test_config_parsing();

#if OCUDU_GPU_CHANNEL_HAS_CUDA
  if (has_cuda_gpu()) {
    verify_fanin_bit_exact(16, 23040, true,  false, false, "device_channel_N16_noawgn");
    verify_fanin_bit_exact(16, 23040, true,  true,  false, "device_channel_N16_snr_awgn");
    verify_fanin_bit_exact(16, 23040, true,  false, true,  "device_channel_N16_explicit_rx_awgn");
    verify_fanin_bit_exact(8,  23040, false, true,  false, "host_stage_fallback_N8_snr_awgn");
    test_mimo_4x4_parallel_copy_and_out();
    test_concurrent_multi_rx_nodes();
    std::cout << "test_cuda_prep_parallel: all CPU + CUDA checks passed (bit-exact)\n";
  } else {
    std::cout << "test_cuda_prep_parallel: CPU checks passed (no CUDA device)\n";
  }
#else
  std::cout << "test_cuda_prep_parallel: CPU checks passed (CUDA disabled)\n";
#endif
  return 0;
}
