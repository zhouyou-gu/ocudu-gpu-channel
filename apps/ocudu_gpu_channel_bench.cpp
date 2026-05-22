#include "app_support.h"
#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/latency.h"
#include "ocudu_gpu_channel/processing.h"
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>

namespace {

void usage()
{
  std::cout << "usage: ocudu-gpu-channel-bench --config topology.yaml [--duration 10s] [--scs-khz 30]\n"
            << "  Drives ChannelProcessor::process_superposition() once per RX node per slot,\n"
            << "  the same call the live broker makes. One fused H2D + kernel + D2H on CUDA\n"
            << "  regardless of edge count.\n";
}

void add_us(ocg::LatencyRecorder& recorder, double value_us)
{
  recorder.add(std::chrono::nanoseconds(static_cast<std::int64_t>(value_us * 1000.0)));
}

void print_summary_row(const std::string& metric,
                       const ocg::LatencySummary& summary,
                       double slot_us,
                       const std::string& gate)
{
  std::cout << metric << "," << summary.count << "," << std::fixed << std::setprecision(3) << summary.p50_us << ","
            << summary.p95_us << "," << summary.p99_us << "," << summary.p999_us << "," << summary.max_us << ","
            << slot_us << "," << gate << "\n";
}

} // namespace

int main(int argc, char** argv)
{
  std::string config_path;
  std::chrono::milliseconds duration = std::chrono::seconds(10);
  unsigned scs_khz = 30;

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") {
      usage();
      return 0;
    }
    if (arg == "--config" && i + 1 < argc) {
      config_path = argv[++i];
    } else if (arg == "--duration" && i + 1 < argc) {
      duration = ocg::app::parse_duration(argv[++i]);
    } else if (arg == "--scs-khz" && i + 1 < argc) {
      scs_khz = static_cast<unsigned>(std::stoul(argv[++i]));
    } else {
      std::cerr << "unknown or incomplete argument: " << arg << "\n";
      usage();
      return 2;
    }
  }

  if (config_path.empty()) {
    usage();
    return 2;
  }

  try {
    auto config = ocg::load_config_file(config_path);
    auto processor = ocg::create_channel_processor(config);

    std::unordered_map<std::string, ocg::IqBuffer> latest_tx;
    for (const auto& device : config.devices) {
      const std::size_t n = ocg::resolve_batch_samples(config.runtime, device.sample_rate_hz);
      ocg::IqBuffer samples(n);
      for (std::size_t i = 0; i != n; ++i) {
        samples[i] = {static_cast<float>((i % 17) / 17.0), static_cast<float>((i % 23) / 23.0)};
      }
      latest_tx.emplace(device.id, std::move(samples));
    }

    ocg::LatencyRecorder recorder;
    ocg::LatencyRecorder h2d_recorder;
    ocg::LatencyRecorder kernel_recorder;
    ocg::LatencyRecorder d2h_recorder;
    ocg::LatencyRecorder gpu_process_recorder;
    std::unordered_map<std::string, ocg::IqBuffer> mixed_by_destination;
    for (const auto& device : config.devices) {
      mixed_by_destination[device.id].resize(ocg::resolve_batch_samples(config.runtime, device.sample_rate_hz));
    }

    // Precompute the SuperpositionInput template per RX device. Inside the hot
    // loop we only need to re-point `samples` at the current latest_tx batch.
    std::unordered_map<std::string, std::vector<ocg::SuperpositionInput>> sup_by_destination;
    for (const auto& device : config.devices) {
      std::vector<ocg::SuperpositionInput> edges;
      for (const auto& link : config.links) {
        if (link.to != device.id) {
          continue;
        }
        const auto* model = ocg::find_model(config, link.model);
        if (model == nullptr) {
          continue;
        }
        edges.push_back({.link_key = ocg::link_key(link), .model = model, .samples = {}});
      }
      sup_by_destination[device.id] = std::move(edges);
    }

    const auto deadline = std::chrono::steady_clock::now() + duration;
    std::uint64_t iterations = 0;
    while (std::chrono::steady_clock::now() < deadline) {
      for (const auto& device : config.devices) {
        const auto start = std::chrono::steady_clock::now();
        auto& mixed = mixed_by_destination[device.id];

        // Fused path: one process_superposition call per RX node per slot,
        // matching the broker. The CUDA backend issues one H2D + one kernel
        // + one D2H regardless of edge count; the CPU backend loops per edge
        // internally and sums.
        auto sup_it = sup_by_destination.find(device.id);
        if (sup_it != sup_by_destination.end() && !sup_it->second.empty()) {
          for (auto& edge : sup_it->second) {
            auto tx_it = latest_tx.find(edge.link_key.substr(0, edge.link_key.find('>')));
            if (tx_it != latest_tx.end()) {
              edge.samples = std::span<const ocg::IqSample>(tx_it->second.data(), mixed.size());
            }
          }
          const auto* rx = device.rx_model.empty() ? nullptr : ocg::find_model(config, device.rx_model);
          processor->process_superposition(device.id, sup_it->second, rx, device.sample_rate_hz,
                                           std::span<ocg::IqSample>(mixed.data(), mixed.size()));
          const auto timings = processor->last_timings();
          if (config.runtime.backend == ocg::Backend::Cuda) {
            add_us(h2d_recorder, timings.h2d_us);
            add_us(kernel_recorder, timings.kernel_us);
            add_us(d2h_recorder, timings.d2h_us);
            add_us(gpu_process_recorder, timings.gpu_process_us);
          }
        }
        recorder.add(std::chrono::steady_clock::now() - start);
        ++iterations;
      }
    }

    const auto summary = recorder.summarize();
    const double slot_us = ocg::nr_slot_duration_us(scs_khz);
    const std::string color = ocg::feasibility_color(summary.p99_us, slot_us, true);

    std::cout << "metric,count,p50_us,p95_us,p99_us,p999_us,max_us,slot_us,gate\n";
    print_summary_row("model_mix_latency", summary, slot_us, color);
    if (config.runtime.backend == ocg::Backend::Cuda) {
      print_summary_row("h2d_us", h2d_recorder.summarize(), slot_us, "n/a");
      print_summary_row("kernel_us", kernel_recorder.summarize(), slot_us, "n/a");
      print_summary_row("d2h_us", d2h_recorder.summarize(), slot_us, "n/a");
      print_summary_row("gpu_process_us", gpu_process_recorder.summarize(), slot_us, "n/a");
    }
    std::cout << "backend," << processor->backend_name() << "\n";
    std::cout << "cuda_status," << ocg::backend_status() << "\n";
    std::cout << "iterations," << iterations << "\n";
    std::cout << "raw_cf32_full_duplex_bits_per_device,rate_hz,bits_per_second\n";
    for (const auto& device : config.devices) {
      std::cout << device.id << "," << device.sample_rate_hz << "," << (device.sample_rate_hz * 128ULL) << "\n";
    }
  } catch (const std::exception& e) {
    std::cerr << "event=fatal error=\"" << e.what() << "\"\n";
    return 1;
  }

  return 0;
}
