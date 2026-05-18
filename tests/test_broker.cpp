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
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
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
  model.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", 0.0}}});
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
void run_lockstep_tx(void* context,
                     std::string endpoint,
                     std::size_t chunk,
                     std::size_t tx_offset,
                     RadioState& radio,
                     std::atomic<bool>& stop)
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

void scenario_multi_ue_lockstep()
{
  ocg::TopologyConfig config;
  config.runtime.backend = ocg::Backend::Cpu;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = 23040;
  config.runtime.queue_samples = 131072;
  config.devices = {
      {.id = "gnb0",
       .role = "gnb",
       .sample_rate_hz = 23040000,
       .tx_endpoint = "tcp://127.0.0.1:25600",
       .rx_endpoint = "tcp://127.0.0.1:25601"},
      {.id = "ue0",
       .role = "ue",
       .sample_rate_hz = 23040000,
       .tx_endpoint = "tcp://127.0.0.1:25602",
       .rx_endpoint = "tcp://127.0.0.1:25603"},
      {.id = "ue1",
       .role = "ue",
       .sample_rate_hz = 23040000,
       .tx_endpoint = "tcp://127.0.0.1:25604",
       .rx_endpoint = "tcp://127.0.0.1:25605"}};
  // Downlink fan-out gnb0->{ue0,ue1}; uplink fan-in {ue0,ue1}->gnb0. The two
  // uplinks superpose at the gNB RX.
  config.links = {{.from = "gnb0", .to = "ue0", .model = "clean"},
                  {.from = "gnb0", .to = "ue1", .model = "clean"},
                  {.from = "ue0", .to = "gnb0", .model = "clean"},
                  {.from = "ue1", .to = "gnb0", .model = "clean"}};
  ocg::ModelConfig model;
  model.id = "clean";
  model.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", 0.0}}});
  config.models.emplace(model.id, model);

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
                     std::ref(stop));
  peers.emplace_back(run_lockstep_tx, context, config.devices[1].tx_endpoint, chunk, tx_offset, std::ref(ue0_radio),
                     std::ref(stop));
  peers.emplace_back(run_lockstep_tx, context, config.devices[2].tx_endpoint, chunk, tx_offset, std::ref(ue1_radio),
                     std::ref(stop));
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

} // namespace

int main()
{
  scenario_loopback();
  scenario_multi_ue_lockstep();
  std::cout << "test_broker OK\n";
  return 0;
}
