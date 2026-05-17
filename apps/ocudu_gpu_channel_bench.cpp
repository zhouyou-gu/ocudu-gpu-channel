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
  std::cout << "usage: ocudu-gpu-channel-bench --config topology.yaml [--duration 10s] [--scs-khz 30]\n";
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
    std::unordered_map<std::string, ocg::IqBuffer> processed_by_link;
    for (const auto& device : config.devices) {
      mixed_by_destination[device.id].resize(ocg::resolve_batch_samples(config.runtime, device.sample_rate_hz));
    }
    for (const auto& link : config.links) {
      const auto* destination = ocg::find_device(config, link.to);
      if (destination != nullptr) {
        processed_by_link[ocg::link_key(link)].resize(
            ocg::resolve_batch_samples(config.runtime, destination->sample_rate_hz));
      }
    }

    const auto deadline = std::chrono::steady_clock::now() + duration;
    std::uint64_t iterations = 0;
    while (std::chrono::steady_clock::now() < deadline) {
      for (const auto& device : config.devices) {
        const auto start = std::chrono::steady_clock::now();
        auto& mixed = mixed_by_destination[device.id];
        std::fill(mixed.begin(), mixed.end(), ocg::IqSample{});
        for (const auto& link : config.links) {
          if (link.to != device.id) {
            continue;
          }
          auto tx_it = latest_tx.find(link.from);
          const auto* source = ocg::find_device(config, link.from);
          const auto* model = ocg::find_model(config, link.model);
          if (tx_it == latest_tx.end() || source == nullptr || model == nullptr) {
            continue;
          }
          const std::string key = ocg::link_key(link);
          auto& processed = processed_by_link[key];
          processor->process_into(key, *model, tx_it->second, processed, source->sample_rate_hz);
          const auto timings = processor->last_timings();
          if (config.runtime.backend == ocg::Backend::Cuda) {
            add_us(h2d_recorder, timings.h2d_us);
            add_us(kernel_recorder, timings.kernel_us);
            add_us(d2h_recorder, timings.d2h_us);
            add_us(gpu_process_recorder, timings.gpu_process_us);
          }
          for (std::size_t i = 0; i != mixed.size(); ++i) {
            mixed[i] += processed[i];
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
