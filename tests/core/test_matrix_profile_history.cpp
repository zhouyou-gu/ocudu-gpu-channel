#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/physical_link.h"
#if OCUDU_GPU_CHANNEL_HAS_CUDA
#include "ocudu_gpu_channel/cuda_backend.h"
#endif

#include <cmath>
#include <complex>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <numbers>
#include <stdexcept>
#include <vector>

namespace {
void require(bool ok, const char* message)
{
  if (!ok) throw std::runtime_error(message);
}

struct Fixture {
  int nt, nr;
  ocg::TopologyConfig config;
  std::unique_ptr<ocg::ChannelProcessor> processor;
  ocg::BrokerLinkControl* control;
  ocg::MatrixProfileShadow matrix{};
  std::vector<ocg::IqBuffer> inputs;
  std::vector<ocg::SuperpositionInput> lanes;

  Fixture(int tx, int rx, ocg::Backend backend, bool mixed = false, double initial_delay = 5.0) : nt(tx), nr(rx), inputs(tx, ocg::IqBuffer(8))
  {
    config.runtime.backend = backend;
    config.runtime.batch_samples_auto = false;
    config.runtime.batch_samples = 8;
    config.runtime.queue_samples = 64;
    int port = 3800;
    for (const auto& [name, count] : {std::pair{"tx", nt}, std::pair{"rx", nr}}) {
      ocg::RadioNodeConfig node;
      node.id = name;
      for (int p = 0; p < count; ++p) {
        ocg::DeviceConfig device;
        device.id = std::string(name) + std::to_string(p);
        device.sample_rate_hz = 23040000;
        device.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
        device.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
        node.tx_ports.push_back(device.id);
        node.rx_ports.push_back(device.id);
        config.devices.push_back(device);
      }
      config.radio_nodes.push_back(node);
    }
    ocg::ModelConfig model;
    model.id = "dynamic";
    model.chain.push_back({.type = ocg::ModelStepType::Tdl,
                           .params = {},
                           .taps = {{.delay_samples = initial_delay}},
                           .taps_declared = true});
    config.models.emplace(model.id, model);
    config.links = {{.from = "tx", .to = "rx", .model = model.id},
                    {.from = "rx", .to = "tx", .model = model.id}};
    require(ocg::validate_config(config).empty(), "history fixture must validate");
    if (mixed) {
      ocg::ModelConfig bypass;
      bypass.id = "bypass";
      bypass.chain.push_back({.type=ocg::ModelStepType::PathLoss, .params={{"path_loss_db",100.0}}});
      config.models.emplace(bypass.id, bypass);
      config.links.push_back({.from="tx", .to="rx", .model=bypass.id});
    }
    processor = ocg::create_channel_processor(config);
    for (const auto& lane : ocg::resolve_topology(config).lanes) {
      if (lane.dst_node != "rx") continue;
      lanes.push_back({.link_key = lane.key,
                       .model = ocg::find_model(config, lane.model_id),
                       .samples = inputs[lane.tx_port],
                       .rx_port = lane.rx_port,
                       .tx_port = lane.tx_port});
    }
    control = processor->collect_control_links().at("tx>rx:dynamic");
    matrix.nt = nt;
    matrix.nr = nr;
    matrix.lane_count = nt * nr;
    for (int k = 0; k < matrix.lane_count; ++k) {
      matrix.lanes[k].n_taps = 1;
      matrix.lanes[k].taps[0].delay_samples = 5.0;
    }
  }

  void apply()
  {
    control->shadow_matrix_profile = matrix;
    control->matrix_profile_pending = true;
    control->profile_pending = false;
    control->seqno.fetch_add(1, std::memory_order_release);
  }

  void clear_inputs()
  {
    for (auto& row : inputs) std::fill(row.begin(), row.end(), ocg::IqSample{});
  }

  std::vector<ocg::IqBuffer> run()
  {
    std::vector<ocg::IqBuffer> output(nr, ocg::IqBuffer(8));
    std::vector<std::span<ocg::IqSample>> rows;
    for (auto& row : output) rows.emplace_back(row);
    processor->process_superposition("rx", lanes, nullptr, 23040000, rows);
    return output;
  }

  ocg::TelemetrySnapshot telemetry() { return ocg::read_telemetry_snapshot(*control); }
};

std::vector<ocg::IqBuffer> delayed_output(int nt, int nr, double delay, ocg::Backend backend, bool scalar = false)
{
  Fixture f(nt, nr, backend);
  for (int k = 0; k < f.matrix.lane_count; ++k)
    f.matrix.lanes[k].taps[0].delay_samples = delay;
  if (scalar) {
    f.control->shadow_profile = f.matrix.lanes[0];
    f.control->profile_pending = true;
    f.control->seqno.fetch_add(1, std::memory_order_release);
  } else {
    f.apply();
  }
  for (int i = 0; i < 132; ++i) f.run();
  const auto resets = f.telemetry().warmup_event_seq;
  std::vector<ocg::IqBuffer> result(nr);
  for (int batch = 0; batch < 132; ++batch) {
    f.clear_inputs();
    if (batch == 0) for (int tx = 0; tx < nt; ++tx)
      f.inputs[tx][7-tx] = {float(tx+1), float(tx)*0.25F};
    // Alternate coefficients while echoes remain in the long history.
    if (batch > 0 && !scalar) {
      for (int k = 0; k < f.matrix.lane_count; ++k) {
        f.matrix.lanes[k].taps[0].gain_db = batch % 2 ? -6 : 0;
        f.matrix.lanes[k].taps[0].phase_rad = 0.2 * k;
      }
      f.apply();
    }
    const auto rows = f.run();
    require(f.telemetry().warmup_event_seq == resets, "coefficient updates must preserve long history");
    for (int rx = 0; rx < nr; ++rx) result[rx].insert(result[rx].end(), rows[rx].begin(), rows[rx].end());
  }
  double energy = 0;
  for (const auto& row : result) for (const auto& s : row) energy += ocg::power(s);
  require(energy > 0.1, "long delayed echo must arrive");
  return result;
}

void check_delays(int nt, int nr)
{
  for (double delay : {5., 120., 120.5, 127.5, 128., 128.5, 129., 160., 1022.5, 1023.}) {
    const auto cpu = delayed_output(nt, nr, delay, ocg::Backend::Cpu);
#if OCUDU_GPU_CHANNEL_HAS_CUDA
    const auto gpu = delayed_output(nt, nr, delay, ocg::Backend::Cuda);
    for (int rx = 0; rx < nr; ++rx) for (std::size_t i = 0; i < cpu[rx].size(); ++i) {
      require(std::hypot(cpu[rx][i].i-gpu[rx][i].i, cpu[rx][i].q-gpu[rx][i].q) < 1e-4,
              "long-delay CUDA echo must match CPU");
    }
    if (nt == 1 && nr == 1) {
      const auto scalar_cpu = delayed_output(1, 1, delay, ocg::Backend::Cpu, true);
      const auto scalar_gpu = delayed_output(1, 1, delay, ocg::Backend::Cuda, true);
      for (std::size_t i = 0; i < scalar_cpu[0].size(); ++i)
        require(std::hypot(scalar_cpu[0][i].i-scalar_gpu[0][i].i, scalar_cpu[0][i].q-scalar_gpu[0][i].q) < 1e-4,
                "scalar profile maximum delay must match CPU");
    }
#endif
  }
}

void check_echoes(int nt, int nr, ocg::Backend backend)
{
  Fixture f(nt, nr, backend);
  f.apply();
  f.run(); f.run(); f.run();
  require(f.telemetry().warmup_event_seq == 1, "first matrix must reset once");
  require(f.telemetry().warmup_until_slot == 0, "initial warmup must finish");

  for (int update = 0; update < 6; ++update) {
    f.clear_inputs();
    // Distinct times and complex amplitudes identify every TX contribution.
    for (int tx = 0; tx < nt; ++tx) {
      f.inputs[tx][7 - tx] = {float(tx + 1), float(-tx) * 0.25F};
    }
    f.run();
    f.clear_inputs();
    if (update % 2 == 0) {
      for (int k = 0; k < f.matrix.lane_count; ++k) {
        f.matrix.lanes[k].taps[0].gain_db = -3.0 - k - update;
        f.matrix.lanes[k].taps[0].phase_rad = (k + update + 1) * 0.4;
      }
    } // Odd updates resend an identical profile.
    f.apply();
    const auto output = f.run();
    for (int rx = 0; rx < nr; ++rx) {
      for (int n = 0; n < 8; ++n) {
        std::complex<double> expected{};
        for (int tx = 0; tx < nt; ++tx) {
          if (n != 4 - tx) continue;
          const auto& tap = f.matrix.lanes[rx * nt + tx].taps[0];
          expected += std::complex<double>(tx + 1, -tx * 0.25) *
                      std::polar(std::pow(10.0, tap.gain_db / 20.0), tap.phase_rad);
        }
        const auto actual = output[rx][n];
        require(std::abs(actual.i - expected.real()) <= 1e-3 &&
                    std::abs(actual.q - expected.imag()) <= 1e-3,
                "delayed echo must survive a gain/phase or identical matrix update");
      }
    }
    require(f.telemetry().warmup_event_seq == 1, "coefficient update must not start warmup");
    require(f.telemetry().warmup_until_slot == 0, "preserved history must remain usable");
  }

  // A path-layout change still discards the old tail for every lane.
  for (int tx = 0; tx < nt; ++tx) f.inputs[tx][7] = {1.0F, 0.0F};
  f.run(); f.clear_inputs();
  f.matrix.lanes[0].taps[0].delay_samples = 6.0;
  f.apply();
  for (const auto& row : f.run()) {
    for (const auto& value : row) {
      require(std::abs(value.i) < 1e-6 && std::abs(value.q) < 1e-6,
              "layout change must clear the old tail across all lanes");
    }
  }
  require(f.telemetry().warmup_event_seq == 2, "delay change must start warmup");
  const auto until = f.telemetry().warmup_until_slot;
  f.matrix.lanes[0].taps[0].gain_db -= 1.0;
  f.apply(); f.run();
  require(f.telemetry().warmup_event_seq == 2, "coefficient update must not restart warmup");
  require(f.telemetry().warmup_until_slot == until, "coefficient update must not end warmup early");
  f.run();
  require(f.telemetry().warmup_until_slot == 0, "original warmup must end on schedule");

  f.matrix.lanes[0].n_taps = 2;
  f.matrix.lanes[0].taps[1].delay_samples = 2.0;
  f.apply(); f.run();
  require(f.telemetry().warmup_event_seq == 3, "tap-count change must start warmup");
  // The existing scalar profile_swap still resets, including when resent.
  f.control->matrix_profile_pending = false;
  f.control->profile_pending = true;
  f.control->shadow_profile = f.matrix.lanes[0];
  for (int i = 0; i < 2; ++i) {
    f.control->seqno.fetch_add(1, std::memory_order_release);
    f.run();
    require(f.telemetry().warmup_event_seq == std::uint64_t(4 + i),
            "scalar profile swap must retain reset behavior");
  }
  f.apply(); f.run();
  require(f.telemetry().warmup_event_seq == 6, "returning from scalar to matrix must reset");
}

// Exercise every non-coefficient field at the shared link decision, including
// fields the matrix wire protocol currently defaults instead of exposing.
void check_settings()
{
  const auto check = [](auto mutate) {
    auto link = std::make_unique<ocg::PhysicalLinkRuntime>();
    link->chain_has_leading_tdl = true;
    link->control.matrix_profile_pending = true;
    auto& matrix = link->control.shadow_matrix_profile;
    matrix.nt = matrix.nr = matrix.lane_count = 1;
    matrix.lanes[0].n_taps = 2;
    matrix.lanes[0].taps[0].delay_samples = 5.0;
    matrix.lanes[0].taps[1].delay_samples = 7.0;
    link->control.seqno.fetch_add(1);
    ocg::snap_physical_link(*link, "test", 8);
    mutate(matrix);
    link->control.seqno.fetch_add(1);
    ocg::snap_physical_link(*link, "test", 8);
    require(ocg::read_telemetry_snapshot(link->control).warmup_event_seq == 2,
            "non-coefficient setting changes must reset");
  };
  check([](auto& m) { m.nt = 2; });
  check([](auto& m) { m.nr = 2; });
  check([](auto& m) { m.lane_count = 2; });
  check([](auto& m) { m.lanes[0].n_taps = 1; });
  check([](auto& m) { m.lanes[0].taps[0].delay_samples = std::nextafter(5.0, 6.0); });
  check([](auto& m) { std::swap(m.lanes[0].taps[0], m.lanes[0].taps[1]); });
  check([](auto& m) { m.lanes[0].taps[0].is_los = true; });
  check([](auto& m) { m.lanes[0].taps[0].los_k_db = 1; });
  check([](auto& m) { m.lanes[0].taps[0].los_angle_rad = 1; });
  check([](auto& m) { m.lanes[0].fading_enabled = true; });
  check([](auto& m) { m.lanes[0].fading_f_d_max_hz = 1; });
  check([](auto& m) { m.lanes[0].fading_spectrum = 1; });
  check([](auto& m) { m.lanes[0].fading_grid_us = 200; });
  check([](auto& m) { m.lanes[0].force = true; });
}
} // namespace

int main()
{
  try {
    for (const auto& [nt, nr] : {std::pair{1, 1}, {2, 1}, {1, 2}, {4, 1}, {1, 4}}) {
      check_echoes(nt, nr, ocg::Backend::Cpu);
      check_delays(nt, nr);
#if OCUDU_GPU_CHANNEL_HAS_CUDA
      require(ocg::cuda_runtime_probe(), "CUDA build must exercise a real GPU");
      check_echoes(nt, nr, ocg::Backend::Cuda);
      Fixture mixed(nt, nr, ocg::Backend::Cuda, true);
      require(!mixed.control->matrix_profile_supported, "mixed CUDA receiver must reject matrix control");
      mixed.inputs[0][0] = {1,0};
      const auto static_output = mixed.run();
      require(ocg::power(static_output[0][5]) > 0.9, "static mixed topology must still work");
#endif
    }
    check_settings();
#if OCUDU_GPU_CHANNEL_HAS_CUDA
    bool capacity_rejected = false;
    try { Fixture too_long(1, 1, ocg::Backend::Cuda, false, 1500.0); }
    catch (const std::runtime_error& e) { capacity_rejected = std::string(e.what()).find("capacity") != std::string::npos; }
    require(capacity_rejected, "oversized device construction must fail explicitly");
#endif
    std::cout << "matrix history: all five antenna shapes passed; CUDA="
              << OCUDU_GPU_CHANNEL_HAS_CUDA << '\n';
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << '\n';
    return 1;
  }
}
