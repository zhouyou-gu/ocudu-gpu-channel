#include "app_support.h"
#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/broker.h"
#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/control_server.h"
#include <chrono>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

namespace {

void usage()
{
  std::cout << "usage: ocudu-gpu-channel --config topology.yaml [--duration 60s] [--strict-realtime] "
               "[--control-endpoint tcp://*:5559] "
               "[--telemetry-endpoint tcp://*:5560 --telemetry-rate-hz 20] "
               "[--hardware-strict] [--control-warmup-cap-slots N]\n";
}

} // namespace

int main(int argc, char** argv)
{
  std::string config_path;
  std::chrono::milliseconds duration{0};
  bool strict_realtime = false;
  std::string control_endpoint;     // empty = control plane disabled
  std::string telemetry_endpoint;   // empty = telemetry feed disabled (v3.0)
  double      telemetry_rate_hz = 20.0;
  bool        hardware_strict = false;  // v3.2 opt-in
  int         warmup_cap_slots = 3;     // v2.2 follow-on; 0 = disabled

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
    } else if (arg == "--strict-realtime") {
      strict_realtime = true;
    } else if (arg == "--control-endpoint" && i + 1 < argc) {
      control_endpoint = argv[++i];
    } else if (arg == "--telemetry-endpoint" && i + 1 < argc) {
      telemetry_endpoint = argv[++i];
    } else if (arg == "--telemetry-rate-hz" && i + 1 < argc) {
      telemetry_rate_hz = std::atof(argv[++i]);
    } else if (arg == "--hardware-strict") {
      hardware_strict = true;
    } else if (arg == "--control-warmup-cap-slots" && i + 1 < argc) {
      warmup_cap_slots = std::atoi(argv[++i]);
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
    std::cout << "event=start backend=" << ocg::to_string(config.runtime.backend)
              << " cuda_status=" << ocg::backend_status() << "\n";

    // v3.2: hardware probe + footprint check. Skipped for CPU backend
    // (no GPU to probe; the CPU path is well-behaved on memory).
    if (config.runtime.backend == ocg::Backend::Cuda) {
      const auto hw = ocg::probe_cuda_hardware(config.runtime.gpu_device);
      if (!hw.ok) {
        std::cout << "event=hardware_probe ok=false device=" << config.runtime.gpu_device
                  << " error=\"" << hw.error << "\"\n";
        // Don't return here — create_channel_processor will give a clearer
        // error if cuda was requested but isn't available. The v3.2 promise
        // is "surface the typed reason"; we've done that.
      } else {
        std::cout << "event=hardware_probe ok=true device=" << hw.device_id
                  << " name=\"" << hw.name << "\""
                  << " sm=" << hw.sm_major << "." << hw.sm_minor
                  << " mem_bytes=" << hw.total_mem_bytes
                  << " driver_version=" << hw.driver_version
                  << " runtime_version=" << hw.runtime_version << "\n";

        const auto target = ocg::kernel_target_sm();
        if (target.major > 0) {
          const int runtime_sm  = hw.sm_major * 10 + hw.sm_minor;
          const int kernel_sm   = target.major * 10 + target.minor;
          if (runtime_sm < kernel_sm) {
            if (hardware_strict) {
              std::cerr << "event=fatal reason=\"hardware-strict: runtime SM "
                        << hw.sm_major << "." << hw.sm_minor
                        << " below kernel target SM "
                        << target.major << "." << target.minor << "\"\n";
              return 1;
            }
            std::cout << "event=hardware_warning reason=\"runtime SM "
                      << hw.sm_major << "." << hw.sm_minor
                      << " below kernel target SM "
                      << target.major << "." << target.minor
                      << "; kernel launches may fail\"\n";
          }
        }

        // Footprint check: reject if topology would exceed 80% of memory.
        const std::uint64_t footprint = ocg::estimate_cuda_device_footprint_bytes(config);
        if (hw.total_mem_bytes > 0 && footprint > (hw.total_mem_bytes * 4ULL) / 5ULL) {
          std::cerr << "event=fatal reason=\"hardware: topology footprint "
                    << footprint << " B exceeds 80% of GPU memory "
                    << hw.total_mem_bytes << " B\"\n";
          return 1;
        }
        std::cout << "event=hardware_footprint estimated_bytes=" << footprint
                  << " total_mem_bytes=" << hw.total_mem_bytes << "\n";
      }
    }

    ocg::Broker broker(std::move(config));

    // Phase 3 C3b: optional ZMQ REP control plane. Disabled unless the user
    // passes --control-endpoint. Lifetime spans the broker run; the
    // background thread is joined at scope exit (~ControlServer).
    std::unique_ptr<ocg::ControlServer> control_server;
    if (!control_endpoint.empty()) {
      ocg::ControlServerConfig ccfg;
      ccfg.endpoint = control_endpoint;
      ccfg.telemetry_endpoint = telemetry_endpoint;   // v3.0; empty = disabled
      ccfg.telemetry_rate_hz  = telemetry_rate_hz;
      ccfg.warmup_cap_slots   = warmup_cap_slots;     // v2.2 follow-on
      control_server = std::make_unique<ocg::ControlServer>(
          std::move(ccfg), broker.collect_control_links());
      control_server->start();
      std::cout << "event=control_start endpoint=\"" << control_endpoint << "\"";
      if (!telemetry_endpoint.empty()) {
        std::cout << " telemetry_endpoint=\"" << telemetry_endpoint
                  << "\" telemetry_rate_hz=" << telemetry_rate_hz;
      }
      std::cout << "\n";
    }

    auto stats = broker.run(duration);
    std::cout << "event=stop tx_pulls=" << stats.tx_pulls << " rx_requests=" << stats.rx_requests
              << " rx_starvations=" << stats.rx_starvations << " tx_queue_overflows=" << stats.tx_queue_overflows
              << " tx_sequence_gaps=" << stats.tx_sequence_gaps << " zmq_errors=" << stats.zmq_errors;
    if (control_server) {
      const auto cs = control_server->stats();
      std::cout << " control_msgs_received=" << cs.msgs_received
                << " control_updates_applied=" << cs.updates_applied
                << " control_updates_rejected=" << cs.updates_rejected
                << " control_batches_committed=" << cs.batches_committed
                << " control_batches_aborted="   << cs.batches_aborted
                << " telemetry_frames="          << cs.telemetry_frames
                << " telemetry_drops="           << cs.telemetry_drops
                << " force_inert_warnings="      << cs.force_inert_warnings;
    }
    std::cout << "\n" << std::flush;
    if (strict_realtime &&
        (stats.tx_pulls == 0 || stats.rx_requests == 0 || stats.rx_starvations != 0 || stats.tx_queue_overflows != 0 ||
         stats.tx_sequence_gaps != 0 || stats.zmq_errors != 0)) {
      std::cerr << "event=unstable reason=\"strict realtime counters were nonzero\"\n";
      return 1;
    }
  } catch (const std::exception& e) {
    std::cerr << "event=fatal error=\"" << e.what() << "\"\n";
    return 1;
  }

  return 0;
}
