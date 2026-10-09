// Loopback integration tests for the concurrent relay broker.
//
// scenario_loopback() stands up synthetic ZMQ peers for a two-device topology
// (a REP source per device TX and a REQ sink per device RX), runs the broker
// between them, and asserts that real IQ flowed both ways with every
// strict-realtime counter at zero.
//
// scenario_multi_ue_lockstep() stands up a three-device fan-in/fan-out topology
// (one gNB, two UEs, the two uplinks superposing at the gNB RX) whose synthetic
// peers are true LOCK-STEP radios: each one withholds a TX chunk until it has
// consumed enough RX to be allowed to transmit it, and it transmits in
// sub-batch chunks. A fixed-batch relay dead-locks this topology -- no radio
// can ever fill a full batch on its ring -- so this scenario is the regression
// test for the variable-size relay.

#include "ocudu_gpu_channel/broker.h"
#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include "ocudu_gpu_channel/pacing.h"
#include "ocudu_gpu_channel/runtime_control.h"
#include "ocudu_gpu_channel/timing_metrics.h"
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <fstream>
#include <unistd.h>
#include <iostream>
#include <string>
#include <thread>
#include <vector>
#include <zmq.h>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

void set_timeouts(void* socket)
{
  int timeout = 100;
  int linger = 0;
  zmq_setsockopt(socket, ZMQ_RCVTIMEO, &timeout, sizeof(timeout));
  zmq_setsockopt(socket, ZMQ_SNDTIMEO, &timeout, sizeof(timeout));
  zmq_setsockopt(socket, ZMQ_LINGER, &linger, sizeof(linger));
}

void scenario_nominal_timing_crosses_arbitrary_fragment_boundaries()
{
  ocg::ReceiverTimingAccumulator timing(23040, 23040000);
  timing.observe(1, 80.0);
  timing.observe(23038, 100.0);
  timing.observe(10000, 120.0);
  timing.observe(23041, 180.0);

  require(timing.calls.count == 4, "nominal timing lost processing calls");
  require(timing.nominal_slots.count == 2,
          "arbitrary fragments did not reconstruct two complete nominal slots");
  require(timing.pending_samples == 10000,
          "cross-boundary fragment remainder was not retained for the next slot");
  require(std::abs(timing.nominal_slots.max_us - 221.87) < 0.02,
          "fragment call time was not apportioned by overlapping sample count");
  require(std::abs(timing.pending_estimated_us - 78.12) < 0.02,
          "pending fragment time estimate does not preserve the call-time remainder");
  require(timing.fragment_calls == 4, "fragment calls were not counted separately");
  require(timing.fragment_samples == 56080, "fragment samples were not accumulated");
  require(timing.fragment_min() == 1 && timing.fragment_max_samples == 23041,
          "fragment sample-size range is incorrect");
  require(timing.fragmented_nominal_slots == 2,
          "reconstructed slots touched by fragments were not counted");
  require(timing.nominal_slots.deadline_misses == 0,
          "sub-slot call deadlines leaked into nominal-slot verdicts");

  ocg::ReceiverTimingAccumulator real_miss(23040, 23040000);
  real_miss.observe(23040, 1100.0);
  require(real_miss.nominal_slots.count == 1 &&
              real_miss.nominal_slots.deadline_misses == 1,
          "a real full-slot compute deadline miss was hidden");
  require(real_miss.fragment_calls == 0,
          "an aligned nominal call was incorrectly classified as a fragment");
}

// Synthetic device TX: a REP server that answers every pull with a batch of IQ.
void run_source(void* context, std::string endpoint, std::size_t batch, std::atomic<bool>& stop)
{
  void* socket = zmq_socket(context, ZMQ_REP);
  set_timeouts(socket);
  if (zmq_bind(socket, endpoint.c_str()) != 0) {
    std::cerr << "FAIL: source could not bind " << endpoint << "\n";
    std::exit(1);
  }
  const ocg::IqBuffer samples(batch, ocg::IqSample{0.5F, 0.25F});
  const std::size_t bytes = samples.size() * sizeof(ocg::IqSample);
  while (!stop.load()) {
    std::uint8_t dummy = 0;
    if (zmq_recv(socket, &dummy, sizeof(dummy), 0) < 0) {
      continue;
    }
    while (!stop.load() && zmq_send(socket, samples.data(), bytes, 0) < 0) {
      // retry a timed-out send so the REP socket stays in a valid state
    }
  }
  zmq_close(socket);
}

// Synthetic device RX: a REQ client that pulls processed IQ from the broker.
void run_sink(void* context, std::string endpoint, std::atomic<bool>& stop, std::atomic<std::uint64_t>& received)
{
  void* socket = zmq_socket(context, ZMQ_REQ);
  set_timeouts(socket);
  if (zmq_connect(socket, endpoint.c_str()) != 0) {
    std::cerr << "FAIL: sink could not connect " << endpoint << "\n";
    std::exit(1);
  }
  bool awaiting_reply = false;
  while (!stop.load()) {
    if (!awaiting_reply) {
      std::uint8_t dummy = 0;
      if (zmq_send(socket, &dummy, sizeof(dummy), 0) < 0) {
        continue;
      }
      awaiting_reply = true;
    }
    zmq_msg_t msg;
    zmq_msg_init(&msg);
    const int nbytes = zmq_msg_recv(&msg, socket, 0);
    if (nbytes < 0) {
      zmq_msg_close(&msg);
      continue;
    }
    awaiting_reply = false;
    if (nbytes > 0) {
      received.fetch_add(static_cast<std::uint64_t>(nbytes) / sizeof(ocg::IqSample));
    }
    zmq_msg_close(&msg);
  }
  zmq_close(socket);
}

void scenario_loopback()
{
  ocg::TopologyConfig config;
  config.runtime.backend = ocg::Backend::Cpu;
  config.runtime.batch_samples_auto = false;
  // Use OCUDU's real 1 ms slot unit so the test exercises a representative
  // batch size rather than a tiny one whose sub-slot timing is dominated by
  // host scheduler jitter.
  config.runtime.batch_samples = 23040;
  config.runtime.queue_samples = 131072;
  config.devices = {
      {.id = "gnb0",
       .role = "gnb",
       .sample_rate_hz = 23040000,
       .tx_endpoint = "tcp://127.0.0.1:25500",
       .rx_endpoint = "tcp://127.0.0.1:25501"},
      {.id = "ue0",
       .role = "ue",
       .sample_rate_hz = 23040000,
       .tx_endpoint = "tcp://127.0.0.1:25502",
       .rx_endpoint = "tcp://127.0.0.1:25503"}};
  config.links = {{.from = "gnb0", .to = "ue0", .model = "clean"},
                  {.from = "ue0", .to = "gnb0", .model = "clean"}};
  ocg::ModelConfig model;
  model.id = "clean";
  model.chain.push_back({.type = ocg::ModelStepType::Tdl,
                         .params = {},
                         .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                         .taps_declared = true});
  config.models.emplace(model.id, model);

  void* context = zmq_ctx_new();
  std::atomic<bool> stop{false};
  std::atomic<std::uint64_t> gnb_received{0};
  std::atomic<std::uint64_t> ue_received{0};

  std::vector<std::thread> peers;
  peers.emplace_back(run_source, context, config.devices[0].tx_endpoint, config.runtime.batch_samples, std::ref(stop));
  peers.emplace_back(run_source, context, config.devices[1].tx_endpoint, config.runtime.batch_samples, std::ref(stop));
  peers.emplace_back(run_sink, context, config.devices[0].rx_endpoint, std::ref(stop), std::ref(gnb_received));
  peers.emplace_back(run_sink, context, config.devices[1].rx_endpoint, std::ref(stop), std::ref(ue_received));

  ocg::Broker broker(config);
  const auto stats = broker.run(std::chrono::milliseconds(800));
  const auto control_links = broker.collect_control_links();

  stop.store(true);
  for (auto& peer : peers) {
    peer.join();
  }
  zmq_ctx_shutdown(context);
  zmq_ctx_destroy(context);

  std::cout << "loopback: tx_pulls=" << stats.tx_pulls << " rx_requests=" << stats.rx_requests
            << " tx_queue_overflows=" << stats.tx_queue_overflows << " tx_sequence_gaps=" << stats.tx_sequence_gaps
            << " rx_starvations=" << stats.rx_starvations << " zmq_errors=" << stats.zmq_errors
            << " gnb_received=" << gnb_received.load() << " ue_received=" << ue_received.load() << "\n";

  // Data-integrity invariants: the relay must not lose, reorder, or corrupt IQ.
  require(stats.zmq_errors == 0, "broker reported ZMQ errors");
  require(stats.tx_sequence_gaps == 0, "broker reported TX sequence gaps");
  require(stats.tx_queue_overflows == 0, "broker reported TX queue overflows");
  require(stats.tx_pulls > 0, "broker pulled no samples from either device");
  require(stats.rx_requests > 0, "broker served no RX requests");
  require(gnb_received.load() > 0, "gnb0 sink received no samples");
  require(ue_received.load() > 0, "ue0 sink received no samples");
  require(control_links.size() == 2, "broker did not expose both link telemetry controls");
  for (const auto& [link_id, ctl] : control_links) {
    const auto telemetry = ocg::read_telemetry_snapshot(*ctl);
    require(telemetry.processed_samples > 0, "slot timing reports no processed samples");
    require(telemetry.sample_rate_hz == 23040000, "slot timing reports the wrong sample rate");
    require(telemetry.slot_deadline_us > 0.0, "slot timing reports no deadline");
    require(telemetry.channel_process_us > 0.0, "slot timing reports no channel process time");
    require(telemetry.slot_process_count > 0,
            "slot timing reports no cumulative processed-slot count");
    require(telemetry.slot_deadline_miss_count <= telemetry.slot_process_count,
            "slot timing cumulative deadline misses exceed processed slots");
    require(telemetry.slot_process_max_us >= telemetry.channel_process_us,
            "slot timing cumulative maximum is below the latest sample");
    require(telemetry.slot_process_p95_us > 0.0, "slot timing reports no cumulative p95");
    require(telemetry.slot_process_p99_us >= telemetry.slot_process_p95_us,
            "slot timing cumulative p99 is below p95");
    require(telemetry.nominal_slot_samples == config.runtime.batch_samples,
            "nominal timing reports the wrong configured slot size");
    require(telemetry.nominal_slot_count > 0,
            "nominal timing reports no reconstructed slots");
    require(telemetry.nominal_deadline_miss_count <= telemetry.nominal_slot_count,
            "nominal timing deadline misses exceed completed slots");
    const double expected_deadline =
        static_cast<double>(telemetry.processed_samples) * 1'000'000.0 /
        static_cast<double>(telemetry.sample_rate_hz);
    require(std::abs(telemetry.slot_deadline_us - expected_deadline) < 1.0e-6,
            "slot deadline is not derived from the actual IQ window");
    (void)link_id;
  }
  // rx_starvations is a soft real-time signal: it depends on host scheduling,
  // so it is reported here but asserted only by the strict-realtime smoke run
  // on a quiet machine, not by this loopback unit test.
}

// Counters shared between one lock-step radio's TX and RX threads.
struct RadioState {
  std::atomic<std::uint64_t> tx_sent{0};
  std::atomic<std::uint64_t> rx_consumed{0};
};

// Lock-step device TX: a REP server that withholds each sub-batch chunk until
// the radio has consumed enough RX to be allowed to transmit it. The lead the
// radio is granted before any RX flows is `tx_offset`. With `tx_offset` smaller
// than the broker batch, no ring can ever reach a full batch -- so a fixed-batch
// relay dead-locks here and a variable-size relay does not.
//
// `stall_after` (0 = never): once the radio has transmitted that many samples
// it accepts the next pull request and then never answers it -- the shape of
// OCUDU's ZMQ TX channel sitting in "Waiting for data" when its lower PHY has
// stopped producing, which is what the 2026-10-01 multi-UE gates froze on.
void run_lockstep_tx(void* context,
                     std::string endpoint,
                     std::size_t chunk,
                     std::size_t tx_offset,
                     RadioState& radio,
                     std::atomic<bool>& stop,
                     std::size_t stall_after)
{
  void* socket = zmq_socket(context, ZMQ_REP);
  set_timeouts(socket);
  if (zmq_bind(socket, endpoint.c_str()) != 0) {
    std::cerr << "FAIL: lockstep TX could not bind " << endpoint << "\n";
    std::exit(1);
  }
  const ocg::IqBuffer samples(chunk, ocg::IqSample{0.5F, 0.25F});
  const std::size_t bytes = samples.size() * sizeof(ocg::IqSample);
  while (!stop.load()) {
    std::uint8_t dummy = 0;
    if (zmq_recv(socket, &dummy, sizeof(dummy), 0) < 0) {
      continue; // timed out; observe stop
    }
    if (stall_after != 0 && radio.tx_sent.load() >= stall_after) {
      // Radio stalled: the request stays pending and is never answered.
      while (!stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      }
      break;
    }
    // Hold the pull until the lock-step budget allows the next chunk.
    while (!stop.load() && radio.tx_sent.load() + chunk > radio.rx_consumed.load() + tx_offset) {
      std::this_thread::sleep_for(std::chrono::microseconds(50));
    }
    if (stop.load()) {
      break;
    }
    while (!stop.load() && zmq_send(socket, samples.data(), bytes, 0) < 0) {
      // retry a timed-out send so the REP socket stays in a valid state
    }
    radio.tx_sent.fetch_add(chunk);
  }
  zmq_close(socket);
}

// Lock-step device RX: a REQ client that pulls processed IQ and credits the
// radio's RX-consumed counter, releasing more of its lock-step TX budget.
void run_lockstep_rx(void* context,
                     std::string endpoint,
                     RadioState& radio,
                     std::atomic<bool>& stop,
                     std::atomic<std::uint64_t>& received)
{
  void* socket = zmq_socket(context, ZMQ_REQ);
  set_timeouts(socket);
  if (zmq_connect(socket, endpoint.c_str()) != 0) {
    std::cerr << "FAIL: lockstep RX could not connect " << endpoint << "\n";
    std::exit(1);
  }
  bool awaiting_reply = false;
  while (!stop.load()) {
    if (!awaiting_reply) {
      std::uint8_t dummy = 0;
      if (zmq_send(socket, &dummy, sizeof(dummy), 0) < 0) {
        continue;
      }
      awaiting_reply = true;
    }
    zmq_msg_t msg;
    zmq_msg_init(&msg);
    const int nbytes = zmq_msg_recv(&msg, socket, 0);
    if (nbytes < 0) {
      zmq_msg_close(&msg);
      continue;
    }
    awaiting_reply = false;
    if (nbytes > 0) {
      const auto n = static_cast<std::uint64_t>(nbytes) / sizeof(ocg::IqSample);
      radio.rx_consumed.fetch_add(n);
      received.fetch_add(n);
    }
    zmq_msg_close(&msg);
  }
  zmq_close(socket);
}

// S15 relay-path parity: a 2-port gNB node and a 2-port UE node joined by
// fixed 2x2 matrices, lock-step peers whose TX is a deterministic ramp that
// differs per port. The broker runs once with the relay-latency knobs off and
// once with all of them on; the RX streams every port received must be
// bit-identical over their common length. The knobs change how IQ moves
// (spin waits, in-place ring reads/sends, rows written into the RX ring),
// never what it is.
void run_ramp_tx(void* context, std::string endpoint, std::size_t chunk, std::size_t tx_offset, float port_bias,
                 RadioState& radio, std::atomic<bool>& stop)
{
  void* socket = zmq_socket(context, ZMQ_REP);
  set_timeouts(socket);
  if (zmq_bind(socket, endpoint.c_str()) != 0) {
    std::cerr << "FAIL: ramp TX could not bind " << endpoint << "\n";
    std::exit(1);
  }
  ocg::IqBuffer samples(chunk);
  std::uint64_t index = 0;
  while (!stop.load()) {
    std::uint8_t dummy = 0;
    if (zmq_recv(socket, &dummy, sizeof(dummy), 0) < 0) {
      continue;
    }
    while (!stop.load() && radio.tx_sent.load() + chunk > radio.rx_consumed.load() + tx_offset) {
      std::this_thread::sleep_for(std::chrono::microseconds(50));
    }
    if (stop.load()) {
      break;
    }
    for (auto& v : samples) {
      const auto k = static_cast<float>(index % 1024);
      v = {k * 0.0009765625F + port_bias, 0.5F - k * 0.00048828125F};
      ++index;
    }
    while (!stop.load() && zmq_send(socket, samples.data(), samples.size() * sizeof(ocg::IqSample), 0) < 0) {
    }
    radio.tx_sent.fetch_add(chunk);
  }
  zmq_close(socket);
}

void run_recording_rx(void* context, std::string endpoint, RadioState& radio, std::atomic<bool>& stop,
                      ocg::IqBuffer& record)
{
  void* socket = zmq_socket(context, ZMQ_REQ);
  set_timeouts(socket);
  if (zmq_connect(socket, endpoint.c_str()) != 0) {
    std::cerr << "FAIL: recording RX could not connect " << endpoint << "\n";
    std::exit(1);
  }
  bool awaiting_reply = false;
  while (!stop.load()) {
    if (!awaiting_reply) {
      std::uint8_t dummy = 0;
      if (zmq_send(socket, &dummy, sizeof(dummy), 0) < 0) {
        continue;
      }
      awaiting_reply = true;
    }
    zmq_msg_t msg;
    zmq_msg_init(&msg);
    const int nbytes = zmq_msg_recv(&msg, socket, 0);
    if (nbytes < 0) {
      zmq_msg_close(&msg);
      continue;
    }
    awaiting_reply = false;
    const auto n = static_cast<std::size_t>(nbytes) / sizeof(ocg::IqSample);
    const auto* data = static_cast<const ocg::IqSample*>(zmq_msg_data(&msg));
    record.insert(record.end(), data, data + n);
    radio.rx_consumed.fetch_add(n);
    zmq_msg_close(&msg);
  }
  zmq_close(socket);
}

const char* kParityTopology = R"yaml(runtime:
  backend: cpu
  batch_samples: 23040
  queue_samples: 230400
devices:
  - id: gnb0_p0
    role: port
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:25700
    rx_endpoint: tcp://127.0.0.1:25701
  - id: gnb0_p1
    role: port
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:25702
    rx_endpoint: tcp://127.0.0.1:25703
  - id: ue0_p0
    role: port
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:25704
    rx_endpoint: tcp://127.0.0.1:25705
  - id: ue0_p1
    role: port
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:25706
    rx_endpoint: tcp://127.0.0.1:25707
radio_nodes:
  - id: gnb0
    tx_ports:
      - gnb0_p0
      - gnb0_p1
    rx_ports:
      - gnb0_p0
      - gnb0_p1
  - id: ue0
    tx_ports:
      - ue0_p0
      - ue0_p1
    rx_ports:
      - ue0_p0
      - ue0_p1
links:
  - from: gnb0
    to: ue0
    model: m2x2
  - from: ue0
    to: gnb0
    model: m2x2
models:
  m2x2:
    fixed_mimo:
      coefficients:
        - tap: 0
          rx: 0
          tx: 0
          real: 0.7214
          imag: 0.0724
        - tap: 0
          rx: 0
          tx: 1
          real: -0.2790
          imag: -0.1909
        - tap: 0
          rx: 1
          tx: 0
          real: 0.3230
          imag: -0.0999
        - tap: 0
          rx: 1
          tx: 1
          real: 0.7106
          imag: 0.1440
    chain:
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
)yaml";

std::vector<ocg::IqBuffer> run_parity_relay(const ocg::TopologyConfig& config)
{
  constexpr std::size_t chunk = 6000;
  constexpr std::size_t tx_offset = 12000;
  void* context = zmq_ctx_new();
  std::atomic<bool> stop{false};
  std::vector<RadioState> radios(4);
  std::vector<ocg::IqBuffer> records(4);
  std::vector<std::thread> peers;
  for (std::size_t d = 0; d != 4; ++d) {
    peers.emplace_back(run_ramp_tx, context, config.devices[d].tx_endpoint, chunk, tx_offset,
                       0.125F * static_cast<float>(d + 1), std::ref(radios[d]), std::ref(stop));
    peers.emplace_back(run_recording_rx, context, config.devices[d].rx_endpoint, std::ref(radios[d]),
                       std::ref(stop), std::ref(records[d]));
  }
  ocg::Broker broker(config);
  const auto stats = broker.run(std::chrono::milliseconds(800));
  stop.store(true);
  for (auto& peer : peers) {
    peer.join();
  }
  zmq_ctx_shutdown(context);
  zmq_ctx_destroy(context);
  require(stats.zmq_errors == 0 && stats.tx_sequence_gaps == 0, "parity relay reported errors");
  return records;
}

void scenario_relay_knobs_bit_identical()
{
  const std::string path = "/tmp/ocg-test-relay-parity-" + std::to_string(::getpid()) + ".yaml";
  {
    std::ofstream out(path);
    out << kParityTopology;
  }
  const auto config = ocg::load_config_file(path);
  std::remove(path.c_str());
  const char* knobs[] = {"OCG_BROKER_SPIN", "OCG_BROKER_FEWER_COPIES", "OCG_BROKER_DIRECT_ROWS"};
  for (const char* k : knobs) {
    ::setenv(k, "0", 1); // baseline: the pre-S15 relay path
  }
  const auto base = run_parity_relay(config);
  for (const char* k : knobs) {
    ::setenv(k, "1", 1);
  }
  const auto fast = run_parity_relay(config);
  for (const char* k : knobs) {
    ::unsetenv(k);
  }
  std::size_t compared = 0;
  for (std::size_t d = 0; d != 4; ++d) {
    const std::size_t n = std::min(base[d].size(), fast[d].size());
    require(n >= 23040, "parity relay: a port received less than one batch");
    for (std::size_t i = 0; i != n; ++i) {
      if (base[d][i].i != fast[d][i].i || base[d][i].q != fast[d][i].q) {
        std::cerr << "port " << d << " sample " << i << " differs\n";
        require(false, "relay knobs changed the RX stream");
      }
    }
    compared += n;
  }
  std::cout << "relay knobs parity: " << compared << " samples bit-identical across 4 ports\n";
}

// One gNB + two UEs on the CPU backend, endpoints at base_port.. base_port+5.
// Downlink fan-out gnb0->{ue0,ue1}; uplink fan-in {ue0,ue1}->gnb0. The two
// uplinks superpose at the gNB RX.
ocg::TopologyConfig make_multi_ue_lockstep_config(unsigned base_port)
{
  const auto ep = [base_port](unsigned k) { return "tcp://127.0.0.1:" + std::to_string(base_port + k); };
  ocg::TopologyConfig config;
  config.runtime.backend = ocg::Backend::Cpu;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = 23040;
  config.runtime.queue_samples = 131072;
  config.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = ep(0), .rx_endpoint = ep(1)},
      {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = ep(2), .rx_endpoint = ep(3)},
      {.id = "ue1", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = ep(4), .rx_endpoint = ep(5)}};
  config.links = {{.from = "gnb0", .to = "ue0", .model = "clean"},
                  {.from = "gnb0", .to = "ue1", .model = "clean"},
                  {.from = "ue0", .to = "gnb0", .model = "clean"},
                  {.from = "ue1", .to = "gnb0", .model = "clean"}};
  ocg::ModelConfig model;
  model.id = "clean";
  model.chain.push_back({.type = ocg::ModelStepType::Tdl,
                         .params = {},
                         .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                         .taps_declared = true});
  config.models.emplace(model.id, model);
  return config;
}

void scenario_multi_ue_lockstep()
{
  const ocg::TopologyConfig config = make_multi_ue_lockstep_config(25600);

  // Sub-batch chunk (not a divisor of the 23040 batch) and a tx_offset smaller
  // than the batch: both are required to dead-lock a fixed-batch relay.
  constexpr std::size_t chunk = 6000;
  constexpr std::size_t tx_offset = 12000;

  void* context = zmq_ctx_new();
  std::atomic<bool> stop{false};
  RadioState gnb_radio;
  RadioState ue0_radio;
  RadioState ue1_radio;
  std::atomic<std::uint64_t> gnb_received{0};
  std::atomic<std::uint64_t> ue0_received{0};
  std::atomic<std::uint64_t> ue1_received{0};

  std::vector<std::thread> peers;
  peers.emplace_back(run_lockstep_tx, context, config.devices[0].tx_endpoint, chunk, tx_offset, std::ref(gnb_radio),
                     std::ref(stop), std::size_t{0});
  peers.emplace_back(run_lockstep_tx, context, config.devices[1].tx_endpoint, chunk, tx_offset, std::ref(ue0_radio),
                     std::ref(stop), std::size_t{0});
  peers.emplace_back(run_lockstep_tx, context, config.devices[2].tx_endpoint, chunk, tx_offset, std::ref(ue1_radio),
                     std::ref(stop), std::size_t{0});
  peers.emplace_back(run_lockstep_rx, context, config.devices[0].rx_endpoint, std::ref(gnb_radio), std::ref(stop),
                     std::ref(gnb_received));
  peers.emplace_back(run_lockstep_rx, context, config.devices[1].rx_endpoint, std::ref(ue0_radio), std::ref(stop),
                     std::ref(ue0_received));
  peers.emplace_back(run_lockstep_rx, context, config.devices[2].rx_endpoint, std::ref(ue1_radio), std::ref(stop),
                     std::ref(ue1_received));

  ocg::Broker broker(config);
  const auto stats = broker.run(std::chrono::milliseconds(800));

  stop.store(true);
  for (auto& peer : peers) {
    peer.join();
  }
  zmq_ctx_shutdown(context);
  zmq_ctx_destroy(context);

  std::cout << "multi-ue lockstep: tx_pulls=" << stats.tx_pulls << " rx_requests=" << stats.rx_requests
            << " tx_queue_overflows=" << stats.tx_queue_overflows << " tx_sequence_gaps=" << stats.tx_sequence_gaps
            << " zmq_errors=" << stats.zmq_errors << " gnb_received=" << gnb_received.load()
            << " ue0_received=" << ue0_received.load() << " ue1_received=" << ue1_received.load() << "\n";

  // The whole point of the regression: a lock-step three-device fan-in/fan-out
  // topology must keep flowing. A fixed-batch relay would hang here with
  // rx_requests at zero -- no ring ever reaches a full 23040-sample batch.
  require(stats.zmq_errors == 0, "broker reported ZMQ errors");
  require(stats.tx_sequence_gaps == 0, "broker reported TX sequence gaps");
  require(stats.tx_queue_overflows == 0, "broker reported TX queue overflows");
  require(stats.rx_requests > 0, "broker served no RX requests (multi-device relay dead-locked)");
  require(gnb_received.load() > 0, "gnb0 RX received no superposed uplink");
  require(ue0_received.load() > 0, "ue0 RX received no downlink");
  require(ue1_received.load() > 0, "ue1 RX received no downlink");
}

// Fail-fast regression for the 2026-10-01 multi-UE wedge: the same lock-step
// topology, but the gNB radio stops transmitting part-way (its REP accepts the
// pull and never answers, as OCUDU's TX channel does once its lower PHY has
// stalled). The UEs then consume the downlink already in flight, pre-transmit
// their lock-step lead, and block on RX; every ring drains to zero and the
// relay freezes with every puller in recv_reply. Without the detector the
// broker would sit like that for the whole duration and report a clean
// event=stop; with OCG_BROKER_WEDGE_TIMEOUT_MS it must end the run early,
// flag relay_wedged and fail the strict counters.
void scenario_multi_ue_lockstep_wedge_fails_fast()
{
  const ocg::TopologyConfig config = make_multi_ue_lockstep_config(25610);
  constexpr std::size_t chunk = 6000;
  constexpr std::size_t tx_offset = 12000;
  constexpr std::size_t gnb_stall_after = 20 * chunk; // ~5 ms of virtual time

  void* context = zmq_ctx_new();
  std::atomic<bool> stop{false};
  RadioState gnb_radio;
  RadioState ue0_radio;
  RadioState ue1_radio;
  std::atomic<std::uint64_t> gnb_received{0};
  std::atomic<std::uint64_t> ue0_received{0};
  std::atomic<std::uint64_t> ue1_received{0};

  std::vector<std::thread> peers;
  peers.emplace_back(run_lockstep_tx, context, config.devices[0].tx_endpoint, chunk, tx_offset, std::ref(gnb_radio),
                     std::ref(stop), gnb_stall_after);
  peers.emplace_back(run_lockstep_tx, context, config.devices[1].tx_endpoint, chunk, tx_offset, std::ref(ue0_radio),
                     std::ref(stop), std::size_t{0});
  peers.emplace_back(run_lockstep_tx, context, config.devices[2].tx_endpoint, chunk, tx_offset, std::ref(ue1_radio),
                     std::ref(stop), std::size_t{0});
  peers.emplace_back(run_lockstep_rx, context, config.devices[0].rx_endpoint, std::ref(gnb_radio), std::ref(stop),
                     std::ref(gnb_received));
  peers.emplace_back(run_lockstep_rx, context, config.devices[1].rx_endpoint, std::ref(ue0_radio), std::ref(stop),
                     std::ref(ue0_received));
  peers.emplace_back(run_lockstep_rx, context, config.devices[2].rx_endpoint, std::ref(ue1_radio), std::ref(stop),
                     std::ref(ue1_received));

  // The knob is read at run(); 300 ms keeps the test short while staying far
  // above the broker's 100 ms socket timeouts and the peers' sleeps.
  setenv("OCG_BROKER_WEDGE_TIMEOUT_MS", "300", 1);
  ocg::Broker broker(config);
  const auto t0 = std::chrono::steady_clock::now();
  const auto stats = broker.run(std::chrono::milliseconds(8000));
  const auto elapsed = std::chrono::steady_clock::now() - t0;
  unsetenv("OCG_BROKER_WEDGE_TIMEOUT_MS");

  stop.store(true);
  for (auto& peer : peers) {
    peer.join();
  }
  zmq_ctx_shutdown(context);
  zmq_ctx_destroy(context);

  std::cout << "multi-ue lockstep wedge: elapsed_ms="
            << std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count()
            << " tx_pulls=" << stats.tx_pulls << " rx_requests=" << stats.rx_requests
            << " relay_wedged=" << stats.relay_wedged << " zmq_errors=" << stats.zmq_errors
            << " gnb_tx_sent=" << gnb_radio.tx_sent.load() << " ue0_received=" << ue0_received.load() << "\n";

  require(stats.rx_requests > 0, "relay never flowed before the gNB stalled");
  require(gnb_radio.tx_sent.load() == gnb_stall_after, "gNB peer did not stall where the scenario said");
  require(stats.relay_wedged == 1, "wedge detector did not flag the frozen relay");
  require(stats.zmq_errors == 1, "wedge was not counted as a transport error (gate/strict path)");
  require(elapsed < std::chrono::milliseconds(4000), "wedge detector did not end the run early");
}

// M5.4 regression: the producer's real-time pacer must not convert an idle
// period into the right to burst. The broker is the clock of a lock-step ZMQ
// radio, and OCUDU's multi-port RX path dead-locks (rather than back-pressures)
// when a burst overruns its circular buffer -- see `pacing.h`. The scenario the
// two-port live gate hit is reproduced here on a synthetic clock: the producer
// publishes one batch, then blocks for a second waiting for the radio to start
// requesting, then resumes.
//
// This scenario is graded on WHEN the pacer stops granting free slots, not on
// wall-clock timing, so it is deterministic and does not sleep.
void scenario_pacer_drops_unrecoverable_debt()
{
  using Clock = ocg::RealTimePacer::Clock;
  const std::uint64_t rate = 23040000; // 23.04 MS/s
  const std::size_t batch = 23040;     // 1 ms
  const auto batch_duration = std::chrono::nanoseconds(1000000);

  ocg::RealTimePacer pacer(rate, batch_duration);
  require(pacer.duration_for(batch) == batch_duration,
          "a full batch must occupy exactly one batch duration at the node rate");

  const Clock::time_point epoch{};
  require(pacer.charge(epoch, batch) <= epoch, "the first slot must not be made to wait");

  // The producer is now blocked on output room for a second: this is the wait
  // for the radio to start requesting, and it is the wait that the pre-fix
  // pacer capitalised into ~1000 free batches.
  const Clock::time_point resumed = epoch + std::chrono::seconds(1);
  std::size_t free_slots = 0;
  for (std::size_t i = 0; i != 4000; ++i) {
    if (pacer.charge(resumed, batch) > resumed) {
      break; // the pacer now demands a wait: the burst is over
    }
    ++free_slots;
  }

  std::cout << "pacer_debt free_slots=" << free_slots << " stall_ms=1000"
            << " max_debt_ms=1\n";

  // One batch of debt plus the slot being charged. A catch-up pacer would have
  // released the whole stall -- about a thousand batches -- back to back.
  require(free_slots == 2, "pacer released a burst proportional to the stall it sat out");

  // Steady state must still be paced at exactly the node rate: no drift, and
  // no free slot once the schedule has caught up with the clock.
  Clock::time_point now = resumed + batch_duration * 2;
  for (std::size_t i = 0; i != 100; ++i) {
    const auto due = pacer.charge(now, batch);
    require(due == now, "steady-state slot did not land on its paced deadline");
    now = due + batch_duration;
  }
}

} // namespace

int main()
{
  scenario_nominal_timing_crosses_arbitrary_fragment_boundaries();
  scenario_relay_knobs_bit_identical();
  scenario_pacer_drops_unrecoverable_debt();
  scenario_loopback();
  scenario_multi_ue_lockstep();
  scenario_multi_ue_lockstep_wedge_fails_fast();
  std::cout << "test_broker OK\n";
  return 0;
}
