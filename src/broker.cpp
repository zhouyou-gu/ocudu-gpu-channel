#include "ocudu_gpu_channel/broker.h"
#include "ocudu_gpu_channel/ring.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstring>
#include <iostream>
#include <memory>
#include <mutex>
#include <span>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <zmq.h>

// Concurrent per-direction relay broker, modelled on srsRAN's GNU Radio
// Companion ZMQ broker (ZMQ REQ source -> Throttle -> channel -> ZMQ REP sink).
//
// Two levels of state. A PortRuntime is one transport port: a ZMQ endpoint
// pair plus the TX ring its puller thread drains (ZMQ REQ, bounded only by
// ring room, exactly as srsRAN's RX channel drains a peer TX). A
// RadioNodeRuntime owns one or more ports and everything with timing in it --
// the sample epoch, the source cursors, the single channel call, the throttle,
// and the output. Every Device lowers to an implicit singleton node until the
// `radio_nodes` schema arrives in M1, so a node is currently its port.
//
// The serving thread is REQ/REP-gated and never zero-fills: it holds a request
// until real processed IQ is ready, and throttles its serve to the node sample
// rate so both directions stay balanced and lock-step.
//
// Every worker publishes a lock-free `WorkerDiag` (current state plus progress
// and stall counters); a once-per-second `event=heartbeat` line reports them so
// a wedged relay can be pinpointed to a single thread from the broker log.

namespace ocg {
namespace {

std::atomic_bool stop_requested = false;

void signal_handler(int)
{
  stop_requested.store(true);
}

// Installs the broker stop-signal handlers for the lifetime of one run() call
// and restores whatever the host process had before, so embedding the broker
// does not permanently hijack SIGINT/SIGTERM.
struct SignalGuard {
  using Handler = void (*)(int);
  Handler prev_int = nullptr;
  Handler prev_term = nullptr;

  SignalGuard()
  {
    prev_int = std::signal(SIGINT, signal_handler);
    prev_term = std::signal(SIGTERM, signal_handler);
  }
  ~SignalGuard()
  {
    std::signal(SIGINT, prev_int);
    std::signal(SIGTERM, prev_term);
  }
  SignalGuard(const SignalGuard&) = delete;
  SignalGuard& operator=(const SignalGuard&) = delete;
};

struct SocketDeleter {
  void operator()(void* socket) const
  {
    if (socket != nullptr) {
      zmq_close(socket);
    }
  }
};

using SocketPtr = std::unique_ptr<void, SocketDeleter>;

struct ContextDeleter {
  void operator()(void* context) const
  {
    if (context != nullptr) {
      zmq_ctx_shutdown(context);
      zmq_ctx_destroy(context);
    }
  }
};

using ContextPtr = std::unique_ptr<void, ContextDeleter>;

void set_int_option(void* socket, int option, int value)
{
  if (zmq_setsockopt(socket, option, &value, sizeof(value)) != 0) {
    throw std::runtime_error(std::string("zmq_setsockopt failed: ") + zmq_strerror(zmq_errno()));
  }
}

SocketPtr make_socket(void* context, int type)
{
  void* socket = zmq_socket(context, type);
  if (socket == nullptr) {
    throw std::runtime_error(std::string("zmq_socket failed: ") + zmq_strerror(zmq_errno()));
  }
  // 100 ms send/receive timeouts so a worker thread wakes periodically to
  // observe the stop flag instead of blocking forever.
  set_int_option(socket, ZMQ_RCVTIMEO, 100);
  set_int_option(socket, ZMQ_SNDTIMEO, 100);
  set_int_option(socket, ZMQ_LINGER, 0);
  set_int_option(socket, ZMQ_RCVHWM, 4);
  set_int_option(socket, ZMQ_SNDHWM, 4);
  return SocketPtr(socket);
}

// Returns true if a sample message was received; false on a timeout (EAGAIN).
bool recv_samples_into(void* socket, std::span<IqSample> out, std::size_t& sample_count)
{
  zmq_msg_t msg;
  zmq_msg_init(&msg);
  const int nbytes = zmq_msg_recv(&msg, socket, 0);
  if (nbytes < 0) {
    const int err = zmq_errno();
    zmq_msg_close(&msg);
    if (err == EAGAIN || err == EINTR) {
      return false;
    }
    throw std::runtime_error(std::string("zmq_msg_recv failed: ") + zmq_strerror(err));
  }
  if (static_cast<std::size_t>(nbytes) % sizeof(IqSample) != 0) {
    zmq_msg_close(&msg);
    throw std::runtime_error("received ZMQ payload is not aligned to cf32 IQ samples");
  }
  sample_count = static_cast<std::size_t>(nbytes) / sizeof(IqSample);
  if (sample_count > out.size()) {
    const std::string detail = "received ZMQ payload samples=" + std::to_string(sample_count) +
                               " exceeds preallocated TX receive buffer samples=" + std::to_string(out.size());
    zmq_msg_close(&msg);
    throw std::runtime_error(detail);
  }
  std::memcpy(out.data(), zmq_msg_data(&msg), static_cast<std::size_t>(nbytes));
  zmq_msg_close(&msg);
  return true;
}

bool send_request(void* socket)
{
  const std::uint8_t dummy = 0;
  const int sent = zmq_send(socket, &dummy, sizeof(dummy), 0);
  if (sent < 0) {
    const int err = zmq_errno();
    if (err == EAGAIN || err == EINTR) {
      return false;
    }
    throw std::runtime_error(std::string("zmq_send request failed: ") + zmq_strerror(err));
  }
  return true;
}

bool send_samples(void* socket, std::span<const IqSample> samples)
{
  const auto nbytes = samples.size() * sizeof(IqSample);
  const int sent = zmq_send(socket, samples.data(), nbytes, 0);
  if (sent < 0) {
    const int err = zmq_errno();
    if (err == EAGAIN || err == EINTR) {
      return false;
    }
    throw std::runtime_error(std::string("zmq_send samples failed: ") + zmq_strerror(err));
  }
  return static_cast<std::size_t>(sent) == nbytes;
}

// One transport port's broker-side state: a REQ socket draining the port's TX
// into a ring, and a REP socket feeding the port's RX. This is exactly the
// pre-MIMO `Device` -- a ZMQ endpoint pair plus its TX ring. A port owns no
// timing: the sample epoch, the throttle, and the channel call belong to the
// RadioNode that owns the port.
struct PortRuntime {
  const DeviceConfig* config = nullptr;
  SocketPtr tx_req;
  SocketPtr rx_rep;
  IqRing tx_ring;
  std::mutex ring_mutex;
  std::size_t batch = 0;
  // Index of the owning RadioNodeRuntime, and this port's position in that
  // node's tx/rx port lists (the canonical matrix index from M1 onwards).
  std::size_t node_index = 0;
  int tx_port = 0;
  int rx_port = 0;
};

// A RadioNode: the owner of a common sample epoch, cursor set, throttle, and
// output for one or more transport ports.
//
// M0 has no `radio_nodes` schema, so every Device lowers to an implicit
// singleton node holding exactly one port as both tx port 0 and rx port 0.
// That lowering is what makes the pre-MIMO semantics survive verbatim: with
// Nt = Nr = 1 a node IS its port. M1 adds the parser that can put several
// ports under one node; nothing else in this struct has to change.
struct RadioNodeRuntime {
  std::string id;
  // M0: the singleton port's DeviceConfig supplies the node-level parameters
  // (sample rate, rx_model). M1 promotes these to the RadioNode block and
  // validates that sibling ports agree.
  const DeviceConfig* config = nullptr;
  std::vector<std::size_t> tx_ports; // port indices, canonical matrix order
  std::vector<std::size_t> rx_ports;
  std::uint64_t sample_rate_hz = 0;
  std::size_t batch = 0;
  const ModelConfig* rx_model = nullptr;
  // Indices into `links` of every lane terminating on this node.
  std::vector<std::size_t> incoming;
};

// One lane's runtime state -- the (rx_port, tx_port) pair of one physical
// link. The cursor is advanced by the destination node's serving thread and
// read by the source port's puller thread, so it is atomic.
struct LinkRuntime {
  std::size_t src_index = 0; // source PORT index
  std::size_t dst_index = 0; // destination PORT index
  std::size_t src_node = 0;
  std::size_t dst_node = 0;
  // Canonical matrix indices. Both are 0 for every M0 lane, which is the
  // scalar 1x1 case; M1 expands one physical link into Nt x Nr lanes.
  int rx_port = 0;
  int tx_port = 0;
  const ModelConfig* model = nullptr;
  std::string key; // canonical link_key, precomputed to keep it off the hot path
  std::atomic<std::uint64_t> cursor{0};
  std::atomic<bool> cursor_init{false};
};

struct AtomicStats {
  std::atomic<std::uint64_t> tx_pulls{0};
  std::atomic<std::uint64_t> rx_requests{0};
  std::atomic<std::uint64_t> rx_starvations{0};
  std::atomic<std::uint64_t> tx_queue_overflows{0};
  std::atomic<std::uint64_t> tx_sequence_gaps{0};
  std::atomic<std::uint64_t> zmq_errors{0};
};

// Lock-free live diagnostics for one worker thread. `state` always points at a
// static string literal, so it can be published with a single atomic store and
// read by the heartbeat without locking.
struct WorkerDiag {
  std::atomic<const char*> state{"start"};
  std::atomic<std::uint64_t> progress{0};      // completed pulls / serves
  std::atomic<std::uint64_t> idle_waits{0};    // ZMQ timeouts waiting on the peer
  std::atomic<std::uint64_t> blocked_iters{0}; // puller room stalls / server data spins
  std::atomic<std::uint64_t> last_samples{0};  // samples in the last pull / serve
  // Per-stage CPU timings for the last completed serve, in microseconds.
  // wait_req covers the ZMQ recv of the next request (idle wait); align is the
  // first-serve cursor co-init; read is the common-window computation plus the
  // ring reads and cursor advance; process is the entire processor call (which
  // on the CUDA backend includes the host-side delay, packing, H2D, kernel,
  // D2H -- the GPU sub-phases are emitted separately by event=gpu_timings);
  // throttle is sleep_until duration; send is the ZMQ reply duration. The
  // heartbeat publishes these as event=cpu_stage_timings. Atomically stored as
  // bit-cast uint64 so they can be read lock-free by the heartbeat thread.
  std::atomic<std::uint64_t> last_wait_req_us_bits{0};
  std::atomic<std::uint64_t> last_align_us_bits{0};
  std::atomic<std::uint64_t> last_read_us_bits{0};
  std::atomic<std::uint64_t> last_process_us_bits{0};
  std::atomic<std::uint64_t> last_throttle_us_bits{0};
  std::atomic<std::uint64_t> last_send_us_bits{0};
};

inline void store_us(std::atomic<std::uint64_t>& slot, double us)
{
  std::uint64_t bits;
  std::memcpy(&bits, &us, sizeof(bits));
  slot.store(bits, std::memory_order_relaxed);
}

inline double load_us(const std::atomic<std::uint64_t>& slot)
{
  const std::uint64_t bits = slot.load(std::memory_order_relaxed);
  double us;
  std::memcpy(&us, &bits, sizeof(us));
  return us;
}

} // namespace

Broker::Broker(TopologyConfig config) : config_(std::move(config))
{
  auto errors = validate_config(config_);
  if (!errors.empty()) {
    throw std::runtime_error("invalid topology: " + errors.front());
  }
  processor_ = create_channel_processor(config_);
}

std::unordered_map<std::string, BrokerLinkControl*>
Broker::collect_control_links()
{
  return processor_ ? processor_->collect_control_links() : std::unordered_map<std::string, BrokerLinkControl*>{};
}

BrokerStats Broker::run(std::chrono::milliseconds duration)
{
  stop_requested.store(false);
  SignalGuard signal_guard;

  ContextPtr context(zmq_ctx_new());
  if (context == nullptr) {
    throw std::runtime_error(std::string("zmq_ctx_new failed: ") + zmq_strerror(zmq_errno()));
  }

  AtomicStats stats;

  // Build per-port broker state (one transport port per configured Device).
  std::vector<std::unique_ptr<PortRuntime>> ports;
  ports.reserve(config_.devices.size());
  for (const auto& device : config_.devices) {
    auto port = std::make_unique<PortRuntime>();
    port->config = &device;
    port->batch = resolve_batch_samples(config_.runtime, device.sample_rate_hz);
    port->tx_ring.reset(config_.runtime.queue_samples);
    port->tx_req = make_socket(context.get(), ZMQ_REQ);
    port->rx_rep = make_socket(context.get(), ZMQ_REP);
    if (zmq_connect(port->tx_req.get(), device.tx_endpoint.c_str()) != 0) {
      throw std::runtime_error("failed to connect TX REQ for " + device.id + ": " + zmq_strerror(zmq_errno()));
    }
    if (zmq_bind(port->rx_rep.get(), device.rx_endpoint.c_str()) != 0) {
      throw std::runtime_error("failed to bind RX REP for " + device.id + ": " + zmq_strerror(zmq_errno()));
    }
    std::cout << "event=socket_ready device=" << device.id << " tx_connect=" << device.tx_endpoint
              << " rx_bind=" << device.rx_endpoint << "\n";
    ports.push_back(std::move(port));
  }

  // Implicit singleton lowering: with no `radio_nodes` schema yet (M1), every
  // Device becomes a RadioNode owning exactly that one port as tx port 0 and
  // rx port 0. Node index therefore equals port index throughout M0, and the
  // node-level parameters are read straight off the port's DeviceConfig.
  // Everything downstream is written against the node/port vocabulary, so M1
  // only has to change how this table is built.
  std::vector<RadioNodeRuntime> nodes(ports.size());
  std::unordered_map<std::string, std::size_t> node_by_id;
  node_by_id.reserve(ports.size());
  for (std::size_t p = 0; p != ports.size(); ++p) {
    auto& node = nodes[p];
    node.id = ports[p]->config->id;
    node.config = ports[p]->config;
    node.tx_ports.push_back(p);
    node.rx_ports.push_back(p);
    node.sample_rate_hz = ports[p]->config->sample_rate_hz;
    node.batch = ports[p]->batch;
    node.rx_model =
        node.config->rx_model.empty() ? nullptr : find_model(config_, node.config->rx_model);
    ports[p]->node_index = p;
    ports[p]->tx_port = 0;
    ports[p]->rx_port = 0;
    node_by_id.emplace(node.id, p);
  }

  // Build per-lane runtime state. In M0 a config link is exactly one lane on
  // the (single-port) node pair it names; M1 expands it into Nt x Nr lanes.
  std::vector<LinkRuntime> links(config_.links.size());
  for (std::size_t i = 0; i != config_.links.size(); ++i) {
    const auto& link = config_.links[i];
    const auto* src = find_device(config_, link.from);
    const auto* dst = find_device(config_, link.to);
    const auto* model = find_model(config_, link.model);
    if (src == nullptr || dst == nullptr || model == nullptr) {
      throw std::runtime_error("invalid link: " + link_key(link));
    }
    const auto src_node_it = node_by_id.find(link.from);
    const auto dst_node_it = node_by_id.find(link.to);
    if (src_node_it == node_by_id.end() || dst_node_it == node_by_id.end()) {
      throw std::runtime_error("link endpoint resolves to no radio node: " + link_key(link));
    }
    links[i].src_node = src_node_it->second;
    links[i].dst_node = dst_node_it->second;
    links[i].src_index = nodes[links[i].src_node].tx_ports[0];
    links[i].dst_index = nodes[links[i].dst_node].rx_ports[0];
    links[i].tx_port = ports[links[i].src_index]->tx_port;
    links[i].rx_port = ports[links[i].dst_index]->rx_port;
    links[i].model = model;
    links[i].key = link_key(link);
  }

  // Per-node incoming lane lists, resolved once so no serve walks every link.
  for (std::size_t i = 0; i != links.size(); ++i) {
    nodes[links[i].dst_node].incoming.push_back(i);
  }

  // Live per-worker diagnostics: one entry per port for the puller role, one
  // per node for the serving role. Sized once up front and never resized, so
  // the worker threads and the heartbeat can reference stable elements without
  // synchronisation. (In M0 the two are the same length -- node index equals
  // port index -- but they are indexed distinctly so M0.4 can split them.)
  std::vector<WorkerDiag> puller_diag(ports.size());
  std::vector<WorkerDiag> server_diag(nodes.size());

  const auto report_thread_error = [&stats](const char* role, const std::exception& e) {
    stats.zmq_errors.fetch_add(1);
    stop_requested.store(true);
    std::cerr << "event=error role=" << role << " detail=\"" << e.what() << "\"\n";
  };

  // Puller thread: ZMQ REQ source. Drains port p's TX flat-out (bounded only
  // by ring room), exactly as srsRAN's RX channel drains a peer TX. Unchanged
  // by the node overlay -- pulling is a per-port transport concern.
  const auto run_puller = [&](std::size_t d) {
    WorkerDiag& diag = puller_diag[d];
    try {
      PortRuntime& dev = *ports[d];
      // recv_buf must hold the largest single ZMQ payload the peer can send.
      // The ring capacity is that hard upper bound: a larger payload could
      // never be relayed and is rejected by recv_samples_into().
      IqBuffer recv_buf(std::max<std::size_t>(config_.runtime.queue_samples, dev.batch));
      bool request_outstanding = false;
      std::size_t pending = 0;       // samples pulled but not yet in the ring
      bool pending_counted = false;  // ring-full already counted for this message
      while (!stop_requested.load()) {
        // Release samples consumed by every link reading this device's ring,
        // then check for room.
        std::size_t room = 0;
        {
          std::lock_guard<std::mutex> lk(dev.ring_mutex);
          std::uint64_t min_cursor = dev.tx_ring.next_sequence();
          bool has_consumer = false;
          bool has_outgoing = false;
          for (const auto& link : links) {
            if (link.src_index != d) {
              continue;
            }
            has_outgoing = true;
            if (!link.cursor_init.load()) {
              min_cursor = dev.tx_ring.earliest_sequence();
              continue;
            }
            has_consumer = true;
            min_cursor = std::min(min_cursor, link.cursor.load());
          }
          if (!has_outgoing) {
            dev.tx_ring.discard_before(dev.tx_ring.next_sequence());
          } else if (has_consumer) {
            dev.tx_ring.discard_before(min_cursor);
          }
          room = dev.tx_ring.free_capacity();
        }
        // Land a previously pulled message before pulling the next one. The
        // message is held in recv_buf and retried until it fits, never dropped.
        if (pending > 0) {
          bool pushed = false;
          {
            std::lock_guard<std::mutex> lk(dev.ring_mutex);
            pushed = dev.tx_ring.push(std::span<const IqSample>(recv_buf.data(), pending));
          }
          if (pushed) {
            diag.state.store("push");
            diag.last_samples.store(pending);
            diag.progress.fetch_add(1);
            stats.tx_pulls.fetch_add(1);
            pending = 0;
            pending_counted = false;
          } else {
            diag.state.store("wait_room");
            diag.blocked_iters.fetch_add(1);
            if (!pending_counted) {
              stats.tx_queue_overflows.fetch_add(1);
              pending_counted = true;
            }
            std::this_thread::sleep_for(std::chrono::microseconds(50));
          }
          continue;
        }

        if (room < dev.batch) {
          diag.state.store("wait_room");
          diag.blocked_iters.fetch_add(1);
          std::this_thread::sleep_for(std::chrono::microseconds(50));
          continue;
        }
        if (!request_outstanding) {
          diag.state.store("send_req");
          if (!send_request(dev.tx_req.get())) {
            diag.idle_waits.fetch_add(1);
            continue; // send timed out; retry
          }
          request_outstanding = true;
        }
        diag.state.store("recv_reply");
        std::size_t sample_count = 0;
        if (!recv_samples_into(dev.tx_req.get(), recv_buf, sample_count)) {
          diag.idle_waits.fetch_add(1);
          continue; // receive timed out; the request is still outstanding
        }
        request_outstanding = false;
        pending = sample_count; // hand off to the land-pending branch above
      }
    } catch (const std::exception& e) {
      report_thread_error("puller", e);
    }
    diag.state.store("stopped");
  };

  // Server thread: ZMQ REP sink. Feeds node n's RX with processed IQ, one
  // request at a time, holding the request until real data is ready, and
  // throttling the serve to the node sample rate.
  //
  // M0.3 rehomes this thread's state onto the RadioNode without changing the
  // thread structure: it is still one request-driven thread that selects the
  // window, calls the channel, and replies on the port's REP socket. M0.4
  // splits it into a producer plus a thin PortRepWorker, which is what makes
  // sibling-port cursor alignment structural rather than incidental. Until
  // then a node has exactly one RX port, so the two are equivalent.
  const auto run_server = [&](std::size_t n) {
    WorkerDiag& diag = server_diag[n];
    try {
      RadioNodeRuntime& node = nodes[n];
      if (node.rx_ports.size() != 1) {
        throw std::runtime_error("M0 broker serves exactly one RX port per node: " + node.id);
      }
      PortRuntime& dev = *ports[node.rx_ports[0]];
      const std::vector<std::size_t>& incoming = node.incoming;
      IqBuffer rx_reply(node.batch);
      std::vector<IqBuffer> inputs(incoming.size(), IqBuffer(node.batch));

      // Fixed description of this node's incoming lanes; the `samples` span is
      // repointed at the freshly read ring data each serve. process_superposition
      // shapes every lane through its model and sums them = the node's RX signal.
      std::vector<SuperpositionInput> superposition(incoming.size());
      for (std::size_t k = 0; k != incoming.size(); ++k) {
        superposition[k].link_key = links[incoming[k]].key;
        superposition[k].model = links[incoming[k]].model;
        superposition[k].rx_port = links[incoming[k]].rx_port;
        superposition[k].tx_port = links[incoming[k]].tx_port;
      }

      // Optional receiver model (thermal-noise floor) applied once to the sum.
      const ModelConfig* rx_model = node.rx_model;

      const std::uint64_t rate = std::max<std::uint64_t>(1, node.sample_rate_hz);
      std::chrono::steady_clock::time_point throttle_anchor{};
      std::uint64_t served = 0;
      bool throttle_anchored = false;
      bool epoch_set = false;
      const auto batch_duration = std::chrono::nanoseconds(
          (node.batch * 1000000000ULL) /
          std::max<std::uint64_t>(1, node.sample_rate_hz));
      const auto starvation_deadline = batch_duration * 5;

      while (!stop_requested.load()) {
        diag.state.store("wait_req");
        const auto t_wait_req_start = std::chrono::steady_clock::now();
        std::uint8_t dummy = 0;
        const int received = zmq_recv(dev.rx_rep.get(), &dummy, sizeof(dummy), 0);
        if (received < 0) {
          const int err = zmq_errno();
          if (err == EAGAIN || err == EINTR || err == EFSM) {
            diag.idle_waits.fetch_add(1);
            continue;
          }
          throw std::runtime_error(std::string("rx request failed: ") + zmq_strerror(err));
        }
        const auto t_align_start = std::chrono::steady_clock::now();
        const double wait_req_us =
            std::chrono::duration<double, std::micro>(t_align_start - t_wait_req_start).count();

        // First serve: co-initialise every incoming edge's cursor to its
        // source's earliest buffered sample in one pass. No ring discards
        // anything before its first consumer co-initialises, so this is each
        // ring's true start (sequence 0) -- the superposition still sums every
        // edge from a common epoch. Co-initialising to the live frontier
        // instead would drop whatever head-start IQ the lock-step radios had
        // already buffered; in a multi-device topology that head-start is the
        // radios' only timing slack, and dropping it dead-locks the relay.
        if (!epoch_set) {
          for (std::size_t k = 0; k != incoming.size(); ++k) {
            auto& link = links[incoming[k]];
            PortRuntime& src = *ports[link.src_index];
            std::lock_guard<std::mutex> lk(src.ring_mutex);
            link.cursor.store(src.tx_ring.earliest_sequence());
            link.cursor_init.store(true);
          }
          epoch_set = true;
        }
        const auto t_read_start = std::chrono::steady_clock::now();
        const double align_us =
            std::chrono::duration<double, std::micro>(t_read_start - t_align_start).count();

        // A request was accepted; it must be answered with real processed IQ.
        //
        // Variable-size relay: serve the largest window simultaneously available
        // on EVERY incoming edge, capped at the device batch, instead of blocking
        // until each edge holds a full fixed batch. A fixed-batch serve strands
        // the partial final chunk every lock-step radio leaves on its ring; in a
        // multi-device fan-in/fan-out topology no radio can advance to refill it,
        // so the whole relay dead-locks. Relaying whatever common amount is ready
        // keeps every radio fed and the pipeline moving.
        std::size_t serve = 0;
        if (incoming.empty()) {
          serve = node.batch; // no lanes -> a full batch of zero-fill
        } else {
          const auto wait_start = std::chrono::steady_clock::now();
          bool starvation_counted = false;
          while (!stop_requested.load()) {
            std::size_t common = node.batch;
            for (std::size_t k = 0; k != incoming.size(); ++k) {
              auto& link = links[incoming[k]];
              PortRuntime& src = *ports[link.src_index];
              std::lock_guard<std::mutex> lk(src.ring_mutex);
              std::uint64_t cur = link.cursor.load();
              if (cur < src.tx_ring.earliest_sequence()) {
                cur = src.tx_ring.earliest_sequence();
                link.cursor.store(cur);
                stats.tx_sequence_gaps.fetch_add(1);
              }
              const std::uint64_t avail = src.tx_ring.next_sequence() - cur;
              common = std::min<std::size_t>(common, static_cast<std::size_t>(avail));
            }
            if (common > 0) {
              serve = common;
              break;
            }
            diag.state.store("wait_data");
            diag.blocked_iters.fetch_add(1);
            if (served > 0 && !starvation_counted &&
                std::chrono::steady_clock::now() - wait_start > starvation_deadline) {
              stats.rx_starvations.fetch_add(1);
              starvation_counted = true;
            }
            std::this_thread::sleep_for(std::chrono::microseconds(50));
          }
        }
        if (serve == 0) {
          break; // stop requested while waiting for edge data
        }

        // Read the common serve window from every incoming edge. The window was
        // sized against each ring's live frontier above and a node's cursor is
        // advanced only here, so the ring cannot have shifted under the read.
        for (std::size_t k = 0; k != incoming.size(); ++k) {
          auto& link = links[incoming[k]];
          PortRuntime& src = *ports[link.src_index];
          const std::span<IqSample> window(inputs[k].data(), serve);
          {
            std::lock_guard<std::mutex> lk(src.ring_mutex);
            if (!src.tx_ring.read(link.cursor.load(), window)) {
              throw std::runtime_error("broker serve window vanished from ring: " + link.key);
            }
          }
          superposition[k].samples = std::span<const IqSample>(window.data(), window.size());
          link.cursor.fetch_add(serve);
        }

        // Superpose every incoming edge into the node's RX signal. On the CUDA
        // backend the per-edge channel shaping and the summation run on the GPU.
        // No lock needed: prepare() preallocated this node's processor state, so
        // concurrent process_superposition() calls touch disjoint state.
        const std::span<IqSample> reply(rx_reply.data(), serve);
        diag.state.store("process");
        const auto t_process_start = std::chrono::steady_clock::now();
        const double read_us =
            std::chrono::duration<double, std::micro>(t_process_start - t_read_start).count();
        processor_->process_superposition(node.id, superposition, rx_model, node.sample_rate_hz,
                                          reply);
        const auto t_throttle_start = std::chrono::steady_clock::now();
        const double process_us =
            std::chrono::duration<double, std::micro>(t_throttle_start - t_process_start).count();

        // Throttle: cap the serve cadence at the device sample rate so the
        // lock-step radio runs at real time, not faster.
        if (!throttle_anchored) {
          throttle_anchor = std::chrono::steady_clock::now();
          throttle_anchored = true;
        } else {
          diag.state.store("throttle");
          const auto target = throttle_anchor + std::chrono::nanoseconds((served * 1000000000ULL) / rate);
          std::this_thread::sleep_until(target);
        }
        const auto t_send_start = std::chrono::steady_clock::now();
        const double throttle_us =
            std::chrono::duration<double, std::micro>(t_send_start - t_throttle_start).count();

        diag.state.store("send");
        while (!stop_requested.load() && !send_samples(dev.rx_rep.get(), reply)) {
          // send timed out; retry so the REP socket stays in a valid state
        }
        const double send_us =
            std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t_send_start).count();
        store_us(diag.last_wait_req_us_bits, wait_req_us);
        store_us(diag.last_align_us_bits, align_us);
        store_us(diag.last_read_us_bits, read_us);
        store_us(diag.last_process_us_bits, process_us);
        store_us(diag.last_throttle_us_bits, throttle_us);
        store_us(diag.last_send_us_bits, send_us);
        served += serve;
        // Re-base the throttle origin every ~1 s of served IQ so the
        // (served * 1e9) product cannot overflow on a long-running relay.
        if (served >= rate) {
          throttle_anchor += std::chrono::nanoseconds((served * 1000000000ULL) / rate);
          served = 0;
        }
        diag.last_samples.store(serve);
        diag.progress.fetch_add(1);
        stats.rx_requests.fetch_add(1);
      }
    } catch (const std::exception& e) {
      report_thread_error("server", e);
    }
    diag.state.store("stopped");
  };

  // One puller per port, one server per node. M0's implicit singleton lowering
  // makes those counts equal; M0.4 grows the node side to a producer plus one
  // PortRepWorker per RX port.
  std::vector<std::thread> workers;
  workers.reserve(ports.size() + nodes.size());
  for (std::size_t p = 0; p != ports.size(); ++p) {
    workers.emplace_back(run_puller, p);
  }
  for (std::size_t n = 0; n != nodes.size(); ++n) {
    workers.emplace_back(run_server, n);
  }

  // Heartbeat: once per second, publish each worker's live state so a wedged
  // relay is diagnosable from the broker log without attaching a debugger.
  const auto emit_heartbeat = [&](std::uint64_t elapsed_s) {
    for (std::size_t d = 0; d != ports.size(); ++d) {
      std::size_t ring_size = 0;
      std::size_t ring_cap = 0;
      {
        std::lock_guard<std::mutex> lk(ports[d]->ring_mutex);
        ring_size = ports[d]->tx_ring.size();
        ring_cap = ports[d]->tx_ring.capacity();
      }
      const WorkerDiag& p = puller_diag[d];
      // Server diagnostics belong to the owning node. One port per node in M0,
      // so this stays a 1:1 line per device exactly as before.
      const WorkerDiag& s = server_diag[ports[d]->node_index];
      std::cout << "event=heartbeat t=" << elapsed_s << " dev=" << ports[d]->config->id << " ring="
                << ring_size << "/" << ring_cap << " puller[state=" << p.state.load()
                << " pulls=" << p.progress.load() << " idle=" << p.idle_waits.load()
                << " room_stall=" << p.blocked_iters.load() << " last=" << p.last_samples.load()
                << "] server[state=" << s.state.load() << " serves=" << s.progress.load()
                << " idle=" << s.idle_waits.load() << " data_spin=" << s.blocked_iters.load()
                << " last=" << s.last_samples.load() << "]\n";
    }
    // Channel-processor GPU timings (zero on the CPU backend).
    const ProcessorTimings t = processor_->last_timings();
    std::cout << "event=gpu_timings t=" << elapsed_s << " h2d_us=" << t.h2d_us << " kernel_us=" << t.kernel_us
              << " d2h_us=" << t.d2h_us << "\n";
    // Per-device CPU stage timings from the last completed serve. process_us
    // is the WHOLE processor call -- on the CUDA backend its h2d/kernel/d2h
    // subset is reported separately by event=gpu_timings above. wait_req_us
    // and throttle_us are mostly idle time waiting for the next REP request
    // and the wall-clock anchor respectively; align_us is non-zero only on
    // the first serve.
    for (std::size_t n = 0; n != nodes.size(); ++n) {
      const WorkerDiag& s = server_diag[n];
      std::cout << "event=cpu_stage_timings t=" << elapsed_s << " dev=" << nodes[n].id
                << " wait_req_us=" << load_us(s.last_wait_req_us_bits)
                << " align_us=" << load_us(s.last_align_us_bits)
                << " read_us=" << load_us(s.last_read_us_bits)
                << " process_us=" << load_us(s.last_process_us_bits)
                << " throttle_us=" << load_us(s.last_throttle_us_bits)
                << " send_us=" << load_us(s.last_send_us_bits) << "\n";
    }
    std::cout.flush();
  };

  const auto start = std::chrono::steady_clock::now();
  auto next_heartbeat = start + std::chrono::seconds(1);
  while (!stop_requested.load()) {
    const auto now = std::chrono::steady_clock::now();
    if (duration.count() > 0 && now - start >= duration) {
      break;
    }
    if (now >= next_heartbeat) {
      const auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - start).count();
      emit_heartbeat(static_cast<std::uint64_t>(elapsed));
      next_heartbeat += std::chrono::seconds(1);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  stop_requested.store(true);
  for (auto& worker : workers) {
    worker.join();
  }

  // Final per-device worker breakdown, so the relay outcome is legible even
  // without the heartbeat trail.
  for (std::size_t d = 0; d != ports.size(); ++d) {
    const WorkerDiag& p = puller_diag[d];
    const WorkerDiag& s = server_diag[ports[d]->node_index];
    std::cout << "event=worker_summary dev=" << ports[d]->config->id
              << " puller[pulls=" << p.progress.load() << " idle=" << p.idle_waits.load()
              << " room_stall=" << p.blocked_iters.load() << "]"
              << " server[serves=" << s.progress.load() << " idle=" << s.idle_waits.load()
              << " data_spin=" << s.blocked_iters.load() << "]\n";
  }
  std::cout.flush();

  BrokerStats result;
  result.tx_pulls = stats.tx_pulls.load();
  result.rx_requests = stats.rx_requests.load();
  result.rx_starvations = stats.rx_starvations.load();
  result.tx_queue_overflows = stats.tx_queue_overflows.load();
  result.tx_sequence_gaps = stats.tx_sequence_gaps.load();
  result.zmq_errors = stats.zmq_errors.load();
  return result;
}

} // namespace ocg
