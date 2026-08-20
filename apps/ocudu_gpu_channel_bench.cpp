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

    // M3.7: drive per resolved RADIO NODE, which is what the broker does and
    // what the backends key their state by. Iterating config.devices predates
    // M1's radio_nodes: on a multi-port topology every lookup missed, so the
    // bench span millions of empty iterations and reported a kernel count of
    // zero -- it measured nothing while looking healthy.
    const ocg::ResolvedTopology resolved = ocg::resolve_topology(config);

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
    // Cumulative per-RX power statistics, used by cross-backend matching:
    // sum(|i|^2 + |q|^2) over every sample of every slot, divided by total
    // samples at the end. Cumulative stat converges with iteration count
    // (instead of last-slot snapshot which is noisy under fading).
    std::unordered_map<std::string, double> mixed_power_sum;
    std::unordered_map<std::string, std::uint64_t> mixed_sample_count;
    // One output ROW per RX port of the node, in canonical matrix order.
    std::unordered_map<std::string, std::vector<ocg::IqBuffer>> rows_by_node;
    for (const auto& node : resolved.nodes) {
      const std::size_t n = ocg::resolve_batch_samples(config.runtime, node.sample_rate_hz);
      auto& rows = rows_by_node[node.id];
      rows.assign(std::max<std::size_t>(1, node.rx_ports.size()), ocg::IqBuffer(n));
      for (const auto& port : node.rx_ports) {
        mixed_power_sum[port] = 0.0;
        mixed_sample_count[port] = 0;
      }
    }

    // Precompute the SuperpositionInput template per RX device. Inside the hot
    // loop we only need to re-point `samples` at the current latest_tx batch.
    // Lanes in resolved order, which is the order the backends' row ranges were
    // built from -- the CUDA backend verifies it rather than assuming it.
    std::unordered_map<std::string, std::vector<ocg::SuperpositionInput>> sup_by_destination;
    std::unordered_map<std::string, std::vector<std::string>> src_device_by_destination;
    for (const auto& node : resolved.nodes) {
      std::vector<ocg::SuperpositionInput> lanes;
      std::vector<std::string> sources;
      for (const auto& lane : resolved.lanes) {
        if (lane.dst_node != node.id) {
          continue;
        }
        const auto* model = ocg::find_model(config, lane.model_id);
        if (model == nullptr) {
          continue;
        }
        lanes.push_back({.link_key = lane.key,
                         .model = model,
                         .samples = {},
                         .rx_port = lane.rx_port,
                         .tx_port = lane.tx_port});
        sources.push_back(lane.src_device);
      }
      sup_by_destination[node.id] = std::move(lanes);
      src_device_by_destination[node.id] = std::move(sources);
    }

    const auto deadline = std::chrono::steady_clock::now() + duration;
    std::uint64_t iterations = 0;
    while (std::chrono::steady_clock::now() < deadline) {
      for (const auto& node : resolved.nodes) {
        const auto start = std::chrono::steady_clock::now();
        auto& rows = rows_by_node[node.id];
        const std::size_t count = rows.front().size();

        // Fused path: one process_superposition call per RX node per slot,
        // matching the broker. The CUDA backend issues one H2D + one kernel
        // + one D2H regardless of edge count; the CPU backend loops per edge
        // internally and sums.
        auto sup_it = sup_by_destination.find(node.id);
        if (sup_it != sup_by_destination.end() && !sup_it->second.empty()) {
          const auto& sources = src_device_by_destination[node.id];
          for (std::size_t k = 0; k != sup_it->second.size(); ++k) {
            auto tx_it = latest_tx.find(sources[k]);
            if (tx_it != latest_tx.end()) {
              sup_it->second[k].samples =
                  std::span<const ocg::IqSample>(tx_it->second.data(), count);
            }
          }
          std::vector<std::span<ocg::IqSample>> row_spans;
          row_spans.reserve(rows.size());
          for (auto& row : rows) {
            row_spans.emplace_back(row.data(), count);
          }
          const auto* rx = node.rx_model.empty() ? nullptr : ocg::find_model(config, node.rx_model);
          processor->process_superposition(node.id, sup_it->second, rx, node.sample_rate_hz,
                                           std::span<std::span<ocg::IqSample>>(row_spans));
          // Accumulate per-RX-port cumulative power (cross-backend matching).
          for (std::size_t r = 0; r != rows.size(); ++r) {
            double slot_sum = 0.0;
            for (const auto& s : rows[r]) {
              slot_sum += static_cast<double>(s.i) * s.i + static_cast<double>(s.q) * s.q;
            }
            const std::string& port = r < node.rx_ports.size() ? node.rx_ports[r] : node.id;
            mixed_power_sum[port] += slot_sum;
            mixed_sample_count[port] += rows[r].size();
          }
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
    // Output stats per RX node. avg_power is cumulative across ALL slots
    // (sum of |i|^2 + |q|^2 over every sample of every slot, divided by
    // total samples) so it converges with iteration count and is robust to
    // per-slot fading variance. Last-slot s0/sN samples kept for spot-debug.
    // Used by scripts/remote/perf-backend-compare.sh to verify CPU and CUDA
    // produce statistically equivalent outputs.
    // One line per RX PORT, so a multi-port radio reports each of its rows.
    // The id stays the port's device id, which is what the comparison scripts
    // already key on -- at 1x1 a node IS its port, so the output is unchanged.
    std::cout << "rx_output,device_id,avg_power_cum,sample0_i,sample0_q,sampleN_i,sampleN_q,total_samples\n";
    for (const auto& node : resolved.nodes) {
      const auto& rows = rows_by_node[node.id];
      for (std::size_t r = 0; r != rows.size(); ++r) {
        const std::string& port = r < node.rx_ports.size() ? node.rx_ports[r] : node.id;
        const std::uint64_t total = mixed_sample_count[port];
        const double avg_power = (total == 0) ? 0.0 : mixed_power_sum[port] / static_cast<double>(total);
        const auto& s0 = rows[r].empty() ? ocg::IqSample{0.0F, 0.0F} : rows[r].front();
        const auto& sN = rows[r].empty() ? ocg::IqSample{0.0F, 0.0F} : rows[r].back();
        std::cout << "rx_output," << port << "," << avg_power << ","
                  << s0.i << "," << s0.q << "," << sN.i << "," << sN.q << ","
                  << total << "\n";
      }
    }
  } catch (const std::exception& e) {
    std::cerr << "event=fatal error=\"" << e.what() << "\"\n";
    return 1;
  }

  return 0;
}
