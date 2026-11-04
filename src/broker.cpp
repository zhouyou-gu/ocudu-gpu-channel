#include "ocudu_gpu_channel/broker.h"
#include "ocudu_gpu_channel/timing_metrics.h"
#include "ocudu_gpu_channel/pacing.h"
#include "ocudu_gpu_channel/ring.h"
#include "ocudu_gpu_channel/runtime_control.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <csignal>
#include <cstring>
#include <deque>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <zmq.h>
#include <cstdlib>
#include <sys/prctl.h>
#include <sched.h>

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
// Three thread roles, nodes + 2 x ports in total. A puller per port drains the
// peer TX into that port's ring. A producer per node picks one common window
// across every incoming lane, calls the channel exactly once, advances all
// source cursors by that same count, throttles to the node sample rate, and
// pushes each output row into its RX port's ring. A REP worker per port owns
// only the socket state machine: receive a request, pop a row, reply.
//
// Splitting the window choice away from the request-driven thread is the point:
// with one producer per node there is no second thread that could select a
// different window, so sibling RX ports consuming different sample ranges is
// unrepresentable rather than merely unlikely. Nothing zero-fills -- an empty
// ring means waiting for real processed IQ.
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

// Diagnostics knobs, read once per run from the environment.
//   OCG_BROKER_POLL_US  opt into sleep polling instead of notified ring waits
//                       (ring room, input data, output row); value in us.
//                       Any value also drops the thread's timer slack to 1 us,
//                       so the sleep is not stretched by the default 50 us.
//   OCG_ZMQ_IO_THREADS  ZMQ I/O threads in the broker context (default 1). All
//                       IQ transfers in and out share them, so with one thread
//                       the uplink and downlink messages queue behind each other.
//   OCG_HOP_TRACE_DIR   write one timestamped event per ZMQ transfer and per
//                       producer slot to <dir>/hop-trace.csv at shutdown, to
//                       split the lock-step cycle into its hops.
// Relay-latency knobs (S15). SPIN is off unless set; FEWER_COPIES and
// DIRECT_ROWS are on unless set to 0 (bit-identical, measured faster):
//   OCG_BROKER_SPIN     1: workers never sleep while waiting: they poll their
//                       sockets without blocking and yield the core between
//                       polls, so a handoff between broker threads does not
//                       wait for a sleeping core to wake. Costs the broker's
//                       own cores at 100%; pin the broker (the gate does).
//                       2: spin only on in-process waits (producer input,
//                       REP row); socket receives block, so fewer workers
//                       compete for the broker's cores.
//   OCG_BROKER_SPIN_US  with SPIN, spin at most this long per wait before
//                       falling back to the blocking/sleeping wait (default 0
//                       = spin without limit). Bounds how many workers spin at
//                       once when the broker has fewer cores than workers.
//   OCG_BROKER_FEWER_COPIES
//                       relay IQ with fewer CPU copies: the puller lands the
//                       ZMQ message in the TX ring directly, the producer
//                       reads its window in place from the TX ring, and the
//                       REP worker sends straight from the RX ring (each falls
//                       back to a copy when the range wraps).
//   OCG_BROKER_DIRECT_ROWS
//                       the channel writes each output row straight into its
//                       RX ring storage instead of a staging row that is then
//                       pushed (falls back when the tail space wraps).
// Fail-fast knob:
//   OCG_BROKER_WEDGE_TIMEOUT_MS
//                       relay wedge detector (default 10000; 0 disables). Once
//                       every puller has pulled at least once (every radio has
//                       transmitted), a run in which NO worker -- puller,
//                       producer or REP -- makes progress for this long is
//                       declared wedged: `event=relay_wedged` is logged with
//                       every worker's state, BrokerStats.relay_wedged is set,
//                       zmq_errors is bumped so the gate scripts and the strict
//                       exit path fail, and the run stops. Lock-step radios can
//                       only freeze all together (the 2026-10-01 multi-UE gates
//                       sat 190 s with every puller in recv_reply), so a frozen
//                       relay is never a valid measurement, only a void one.
struct BrokerDiagKnobs {
  std::chrono::microseconds poll{50};
  bool tight_slack = false;
  std::string trace_dir;
  int io_threads = 0;
  bool spin = false;         // in-process waits spin
  bool spin_sockets = false; // socket receives spin too (OCG_BROKER_SPIN=1)
  std::chrono::microseconds spin_budget{0};
  bool fewer_copies = false;
  bool direct_rows = false;
  std::chrono::milliseconds wedge_timeout{10000};
};

// True unless the variable is set to "0".
bool env_default_on(const char* name)
{
  const char* value = std::getenv(name);
  return value == nullptr || std::strcmp(value, "0") != 0;
}

BrokerDiagKnobs read_diag_knobs()
{
  BrokerDiagKnobs knobs;
  if (const char* poll = std::getenv("OCG_BROKER_POLL_US"); poll != nullptr && *poll != '\0') {
    knobs.poll = std::chrono::microseconds(std::strtol(poll, nullptr, 10));
    knobs.tight_slack = true;
  }
  if (const char* io = std::getenv("OCG_ZMQ_IO_THREADS"); io != nullptr && *io != '\0') {
    knobs.io_threads = std::atoi(io);
  }
  if (const char* dir = std::getenv("OCG_HOP_TRACE_DIR"); dir != nullptr) {
    knobs.trace_dir = dir;
  }
  if (const char* spin = std::getenv("OCG_BROKER_SPIN"); spin != nullptr) {
    knobs.spin = std::strcmp(spin, "1") == 0 || std::strcmp(spin, "2") == 0;
    knobs.spin_sockets = std::strcmp(spin, "1") == 0;
  }
  if (const char* budget = std::getenv("OCG_BROKER_SPIN_US"); budget != nullptr && *budget != '\0') {
    knobs.spin_budget = std::chrono::microseconds(std::strtol(budget, nullptr, 10));
  }
  knobs.fewer_copies = env_default_on("OCG_BROKER_FEWER_COPIES");
  knobs.direct_rows = env_default_on("OCG_BROKER_DIRECT_ROWS");
  if (const char* wedge = std::getenv("OCG_BROKER_WEDGE_TIMEOUT_MS"); wedge != nullptr && *wedge != '\0') {
    knobs.wedge_timeout = std::chrono::milliseconds(std::strtol(wedge, nullptr, 10));
  }
  return knobs;
}

// One wait step of a worker whose peer has not produced yet. `since` is when
// the current wait began: with a spin budget the worker spins only that long.
void wait_tick(const BrokerDiagKnobs& knobs,
               std::chrono::steady_clock::time_point since = std::chrono::steady_clock::time_point::max())
{
  if (knobs.spin && (knobs.spin_budget.count() == 0 || since == std::chrono::steady_clock::time_point::max() ||
                     std::chrono::steady_clock::now() - since < knobs.spin_budget)) {
    sched_yield();
  } else {
    std::this_thread::sleep_for(knobs.poll);
  }
}

// Each signal has one waiting worker. Take a ticket BEFORE checking its rings:
// a publication between the empty check and wait must not become a lost wake.
// The timeout is for shutdown and starvation diagnostics, not normal handoff.
class RingProgress {
public:
  std::uint64_t ticket()
  {
    std::lock_guard<std::mutex> lock(mutex_);
    return generation_;
  }

  void notify()
  {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      ++generation_;
    }
    changed_.notify_one();
  }

  void wait(std::uint64_t observed)
  {
    std::unique_lock<std::mutex> lock(mutex_);
    changed_.wait_for(lock, std::chrono::milliseconds(5), [&] {
      return generation_ != observed || stop_requested.load();
    });
  }

private:
  std::mutex mutex_;
  std::condition_variable changed_;
  std::uint64_t generation_ = 0;
};

void wait_for_ring(const BrokerDiagKnobs& knobs, RingProgress& progress,
                   std::uint64_t ticket, std::chrono::steady_clock::time_point since)
{
  // Explicit diagnostic polling/spinning keeps its existing meaning. Normal
  // operation sleeps until a producer/consumer changes a relevant ring.
  if (knobs.spin || knobs.tight_slack) {
    wait_tick(knobs, since);
  } else {
    progress.wait(ticket);
  }
}

// Spin mode's replacement for a blocking receive: poll without blocking,
// yielding between polls, for at most the socket's 100 ms receive timeout so
// the caller still observes its stop flag. `try_once` returns true on success.
// With a spin budget, the poll after the budget blocks on the socket (zmq_poll)
// for the rest of the 100 ms, so an idle worker stops occupying a core.
template <class TryOnce>
bool spin_receive(TryOnce&& try_once, const std::atomic<bool>& stop, void* socket,
                  std::chrono::microseconds budget)
{
  const auto start = std::chrono::steady_clock::now();
  const auto deadline = start + std::chrono::milliseconds(100);
  while (!stop.load(std::memory_order_relaxed)) {
    if (try_once()) {
      return true;
    }
    const auto now = std::chrono::steady_clock::now();
    if (now >= deadline) {
      return false;
    }
    if (budget.count() != 0 && now - start >= budget) {
      zmq_pollitem_t item{socket, 0, ZMQ_POLLIN, 0};
      const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count();
      if (zmq_poll(&item, 1, static_cast<long>(std::max<long long>(1, left))) <= 0) {
        return try_once();
      }
      continue;
    }
    sched_yield();
  }
  return false;
}

void apply_thread_slack(const BrokerDiagKnobs& knobs)
{
  if (knobs.tight_slack) {
    prctl(PR_SET_TIMERSLACK, 1000UL, 0, 0, 0);
  }
}

// Hop trace: preallocated per worker, appended by that worker only, read after
// every worker is joined.
enum class HopEvent : std::uint32_t {
  PullReqSent = 0,
  PullReplyRecv = 1,
  PullRingPushed = 2,
  ProduceDataReady = 10,
  ProducePushed = 11,
  RepReqRecv = 20,
  RepRowPopped = 21,
  RepReplySent = 22,
};

struct HopRecord {
  std::int64_t t_ns;
  std::uint32_t event;
  std::uint32_t samples;
};

struct HopTrace {
  bool enabled = false;
  std::vector<HopRecord> records;

  void reserve(bool on)
  {
    enabled = on;
    if (on) {
      records.reserve(1u << 20);
    }
  }
  void add(HopEvent event, std::size_t samples)
  {
    if (!enabled || records.size() == records.capacity()) {
      return;
    }
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    records.push_back({std::chrono::duration_cast<std::chrono::nanoseconds>(now).count(),
                       static_cast<std::uint32_t>(event), static_cast<std::uint32_t>(samples)});
  }
};

// Returns true if a sample message was received; false on a timeout (EAGAIN).
bool recv_samples_into(void* socket, std::span<IqSample> out, std::size_t& sample_count, int flags = 0)
{
  zmq_msg_t msg;
  zmq_msg_init(&msg);
  const int nbytes = zmq_msg_recv(&msg, socket, flags);
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

// Receives one sample message and keeps it in `msg` (the caller closes it), so
// the payload can be pushed into a ring without an intermediate copy.
bool recv_samples_msg(void* socket, zmq_msg_t& msg, std::size_t max_samples, std::size_t& sample_count,
                      int flags = 0)
{
  const int nbytes = zmq_msg_recv(&msg, socket, flags);
  if (nbytes < 0) {
    const int err = zmq_errno();
    if (err == EAGAIN || err == EINTR) {
      return false;
    }
    throw std::runtime_error(std::string("zmq_msg_recv failed: ") + zmq_strerror(err));
  }
  if (static_cast<std::size_t>(nbytes) % sizeof(IqSample) != 0) {
    throw std::runtime_error("received ZMQ payload is not aligned to cf32 IQ samples");
  }
  sample_count = static_cast<std::size_t>(nbytes) / sizeof(IqSample);
  if (sample_count > max_samples) {
    throw std::runtime_error("received ZMQ payload samples=" + std::to_string(sample_count) +
                             " exceeds the TX ring capacity samples=" + std::to_string(max_samples));
  }
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

// Appends one wire chunk to a bounded capture buffer, honouring the skip
// window. `seen` is the port's absolute sample count in that direction and is
// advanced here, so the recorded window is [skip, skip + limit) of the wire
// stream itself -- the same absolute range on every port, which is what lets a
// checker line an output row up against the input columns that produced it.
void append_capture(IqBuffer& capture, std::size_t limit, std::size_t skip, std::uint64_t& seen,
                    const IqSample* data, std::size_t count)
{
  const std::uint64_t begin = seen;
  seen += count;
  if (limit == 0 || capture.size() >= limit || seen <= skip) {
    return;
  }
  const std::size_t offset = begin >= skip ? 0 : static_cast<std::size_t>(skip - begin);
  const std::size_t take = std::min(count - offset, limit - capture.size());
  capture.insert(capture.end(), data + offset, data + offset + take);
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
  RingProgress tx_room_changed;
  RingProgress rx_data_changed;
  std::size_t batch = 0;
  std::size_t queue_samples = 0; // tx_ring capacity
  // Index of the owning RadioNodeRuntime, and this port's position in that
  // node's tx/rx port lists (the canonical matrix index from M1 onwards).
  std::size_t node_index = 0;
  int tx_port = 0;
  int rx_port = 0;

  // RX output ring: the owning node's producer pushes this port's processed
  // row here and the PortRepWorker pops it. This buffer is new in M0.4 -- the
  // pre-MIMO broker had no RX-side storage and computed a reply on demand --
  // and it sits directly in the latency path, which is why its occupancy is a
  // named M0 exit measurement rather than an implementation detail.
  //
  // Capacity is deliberately larger than the producer's run-ahead bound: the
  // producer may only fill up to `rx_high_water`, and the slack above it is
  // what lets a full batch be pushed while the consumer is still working
  // through the previous one.
  IqRing rx_ring;
  std::mutex rx_mutex;
  std::uint64_t rx_cursor = 0;   // PortRepWorker read cursor
  std::size_t rx_high_water = 0; // producer run-ahead bound, in samples
  // Serve boundaries, in ring order. The producer records the length of every
  // window it publishes here, and the PortRepWorker answers a request with
  // exactly one such window. This is what carries the node's common window all
  // the way to the wire: the producer writes the SAME count to every sibling
  // port in one loop, so consuming those counts in order makes sibling replies
  // identically sized by construction, with no coordination between the REP
  // workers. Sizing a reply from the ring occupancy instead -- the obvious
  // `min(batch, available)` -- reads a quantity that two sibling threads sample
  // at two different instants, and a producer push landing between them splits
  // one common window into two different replies.
  std::deque<std::size_t> rx_slots;
  // High-water occupancy actually reached, sampled by the producer under the
  // ring lock right after each push. The heartbeat samples occupancy once a
  // second, which would miss the peaks that set the real added latency.
  std::atomic<std::size_t> rx_peak_occupancy{0};

  // Wire-boundary capture (see WireCaptureConfig). `tx_capture` belongs to this
  // port's puller thread and `rx_capture` to its REP worker; both are
  // preallocated to `capture_limit` and never grow, so no wire operation
  // allocates. Read only after the workers are joined.
  std::size_t capture_limit = 0;
  std::size_t capture_skip = 0;
  std::uint64_t tx_wire_samples = 0; // puller thread only
  std::uint64_t rx_wire_samples = 0; // REP worker thread only
  IqBuffer tx_capture;
  IqBuffer rx_capture;
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
  // True when this node came from implicit singleton lowering rather than a
  // declared `radio_nodes` block. Every M0 node is implicit; M1 has both kinds
  // and the startup log has to say which, because a port's canonical matrix
  // index means something different in each case.
  bool implicit = true;
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
  std::string physical_key; // shared by every lane of one physical link
  std::atomic<std::uint64_t> cursor{0};
  std::atomic<bool> cursor_init{false};
};

// Run-cumulative relay health counters shared by the worker threads.
struct AtomicStats {
  std::atomic<std::uint64_t> tx_pulls{0};
  std::atomic<std::uint64_t> rx_requests{0};
  std::atomic<std::uint64_t> rx_starvations{0};
  std::atomic<std::uint64_t> tx_queue_overflows{0};
  std::atomic<std::uint64_t> tx_sequence_gaps{0};
  std::atomic<std::uint64_t> zmq_errors{0};
  std::atomic<std::uint64_t> relay_wedged{0};
};

// Lock-free live diagnostics for one worker thread. `state` always points at a
// static string literal, so it can be published with a single atomic store and
// read by the heartbeat without locking.
struct WorkerDiag {
  std::atomic<const char*> state{"start"};
  std::atomic<std::uint64_t> progress{0};      // completed pulls / serves
  std::atomic<std::uint64_t> idle_waits{0};    // ZMQ timeouts waiting on the peer
  std::atomic<std::uint64_t> blocked_iters{0}; // puller room stalls / server data spins
  std::atomic<std::uint64_t> last_samples{0};  // samples in the last pull / slot
  std::atomic<std::int64_t> clock_lag_us{0};
  std::atomic<std::int64_t> clock_max_lag_us{0};
  // Cumulative samples this worker has moved. M5.4 needs it on the puller: a
  // node's sibling TX ports are pulled by INDEPENDENT threads, so one can run
  // ahead of the other, and `last_samples` (one batch) cannot show that. srsRAN
  // aligns its ZMQ TX channels, so a drift here is the kind of thing that
  // deadlocks a real multi-channel radio.
  std::atomic<std::uint64_t> total_samples{0};
  // Per-worker copies of the run-cumulative health counters in AtomicStats,
  // so the heartbeat can print a per-device, per-second delta: `event=stop`
  // only gives the run total, which cannot separate a starvation during
  // attach from one during the measured window. A producer owns
  // `starvations` and `sequence_gaps` (its node's input side); a puller owns
  // `overflows` (its port's TX ring). Relaxed like the other counters.
  std::atomic<std::uint64_t> starvations{0};
  std::atomic<std::uint64_t> sequence_gaps{0};
  std::atomic<std::uint64_t> overflows{0};
  // Per-stage CPU timings for this worker's last completed unit of work, in
  // microseconds. The slots are generic because the roles have different
  // stages: see kProducerStage* and kRepStage* below for the two vocabularies.
  // Atomically stored as bit-cast uint64 so the heartbeat thread can read them
  // lock-free.
  std::array<std::atomic<std::uint64_t>, 8> stage_us_bits{};

  // Per-slot histogram of the producer's process stage.
  //
  // `stage_us_bits` above holds only the LAST completed slot, and the
  // heartbeat samples it once a second. Percentiles taken over those samples
  // describe ~20 slots out of the ~20000 a 20-second run actually processes,
  // so a "p99" computed from them is just the maximum of a 20-point sample and
  // says nothing about the tail that a real-time budget cares about. These
  // buckets are written by the producer on EVERY slot instead, so the summary
  // at shutdown is a percentile over the whole run.
  //
  // Fixed-width buckets of 5 us up to 5 ms, plus an overflow bucket. A slot at
  // 23.04 MS/s is 1000 us, so this resolves the whole budget and five times
  // past it at 0.5% granularity; anything slower than 5 ms is already a gross
  // deadline miss and only its count matters. Relaxed increments are enough:
  // the reader runs after the producers have joined.
  static constexpr std::size_t kProcessBuckets = 1001;
  static constexpr double kProcessBucketUs = 5.0;
  std::array<std::atomic<std::uint64_t>, kProcessBuckets> process_hist{};
};

// Records one slot's process duration into the histogram above.
inline void record_process_us(WorkerDiag& diag, double us)
{
  if (!(us >= 0.0)) {
    return; // NaN or a negative clock reading is not a measurement
  }
  auto bucket = static_cast<std::size_t>(us / WorkerDiag::kProcessBucketUs);
  if (bucket >= WorkerDiag::kProcessBuckets) {
    bucket = WorkerDiag::kProcessBuckets - 1;
  }
  diag.process_hist[bucket].fetch_add(1, std::memory_order_relaxed);
}

// Percentile of the recorded distribution, reported at the upper edge of the
// bucket the rank falls in. The overflow bucket reports its lower edge with the
// caller expected to read it as "at least this".
inline double process_percentile_us(const WorkerDiag& diag, double fraction)
{
  std::uint64_t total = 0;
  for (const auto& bucket : diag.process_hist) {
    total += bucket.load(std::memory_order_relaxed);
  }
  if (total == 0) {
    return 0.0;
  }
  const auto target = static_cast<std::uint64_t>(fraction * static_cast<double>(total));
  std::uint64_t seen = 0;
  for (std::size_t i = 0; i != WorkerDiag::kProcessBuckets; ++i) {
    seen += diag.process_hist[i].load(std::memory_order_relaxed);
    if (seen > target) {
      return static_cast<double>(i + 1) * WorkerDiag::kProcessBucketUs;
    }
  }
  return static_cast<double>(WorkerDiag::kProcessBuckets) * WorkerDiag::kProcessBucketUs;
}

inline std::uint64_t process_sample_count(const WorkerDiag& diag)
{
  std::uint64_t total = 0;
  for (const auto& bucket : diag.process_hist) {
    total += bucket.load(std::memory_order_relaxed);
  }
  return total;
}

// Producer stage slots. room is the wait for output-ring headroom; align is
// the first-slot cursor co-init; data is the wait for a common input window;
// read is the ring reads; process is the entire processor call (which on the
// CUDA backend includes the host-side delay, packing, H2D, kernel, D2H -- the
// GPU sub-phases are emitted separately by event=gpu_timings); throttle is the
// sleep_until duration; push is the handoff into the RX output rings.
enum : std::size_t {
  kProducerStageRoom = 0,
  kProducerStageAlign,
  kProducerStageData,
  kProducerStageRead,
  kProducerStageProcess,
  kProducerStageThrottle,
  kProducerStagePush,
};

// PortRepWorker stage slots. wait_req is the ZMQ recv of the next request (an
// idle wait); pop is the wait for a row to appear in this port's RX ring; send
// is the ZMQ reply duration.
enum : std::size_t {
  kRepStageWaitReq = 0,
  kRepStagePop,
  kRepStageSend,
};

// Samples the producer may still push into this port's RX ring. Bounded by the
// run-ahead high-water mark rather than by raw capacity, so the steady-state
// added latency is the high-water mark and not the whole ring.
std::size_t rx_headroom(PortRuntime& port)
{
  std::lock_guard<std::mutex> lk(port.rx_mutex);
  const std::size_t occupancy = port.rx_ring.size();
  if (occupancy >= port.rx_high_water) {
    return 0;
  }
  return std::min(port.rx_high_water - occupancy, port.rx_ring.free_capacity());
}

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

void Broker::set_wire_capture(WireCaptureConfig capture)
{
  capture_ = std::move(capture);
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
  const BrokerDiagKnobs knobs = read_diag_knobs();

  ContextPtr context(zmq_ctx_new());
  if (context == nullptr) {
    throw std::runtime_error(std::string("zmq_ctx_new failed: ") + zmq_strerror(zmq_errno()));
  }
  if (knobs.io_threads > 0 && zmq_ctx_set(context.get(), ZMQ_IO_THREADS, knobs.io_threads) != 0) {
    throw std::runtime_error(std::string("zmq_ctx_set ZMQ_IO_THREADS failed: ") + zmq_strerror(zmq_errno()));
  }

  AtomicStats stats;

  // Build per-port broker state (one transport port per configured Device).
  std::vector<std::unique_ptr<PortRuntime>> ports;
  ports.reserve(config_.devices.size());
  for (const auto& device : config_.devices) {
    auto port = std::make_unique<PortRuntime>();
    port->config = &device;
    port->batch = resolve_batch_samples(config_.runtime, device.sample_rate_hz);
    port->queue_samples =
        device.tx_queue_samples != 0 ? device.tx_queue_samples : config_.runtime.queue_samples;
    port->tx_ring.reset(port->queue_samples);
    if (!capture_.directory.empty() && capture_.samples_per_port > 0) {
      port->capture_limit = capture_.samples_per_port;
      port->capture_skip = capture_.skip_samples;
      port->tx_capture.reserve(port->capture_limit);
      port->rx_capture.reserve(port->capture_limit);
    }
    // RX output ring. Capacity is `runtime.rx_ring_batches` batches; the
    // producer's run-ahead is capped at one batch regardless, so the steady-
    // state added latency is one batch and the rest is push slack.
    port->rx_ring.reset(config_.runtime.rx_ring_batches * port->batch);
    port->rx_high_water = port->batch;
    port->tx_req = make_socket(context.get(), ZMQ_REQ);
    port->rx_rep = make_socket(context.get(), ZMQ_REP);
    // Data-plane REP only: unbounded send high-water mark.
    //
    // Carried over from the superseded MIMO attempt, where this cost real time
    // to rediscover. With a finite REP send HWM, libzmq 4.3.5's internal
    // non-mandatory ROUTER silently drops the reply at the routing-id envelope
    // stage while zmq_send() still returns success. A strict REQ peer then
    // waits forever for a reply that was never queued. The REQ sockets keep
    // their bounded settings; only this socket is exempt.
    set_int_option(port->rx_rep.get(), ZMQ_SNDHWM, 0);
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

  // Lowering and lane expansion both come from resolve_topology(), so the
  // broker, the CPU backend and the CUDA backend read one definition of the
  // lane set, the lane order and the per-lane state keys. Deriving that here
  // instead would be a silent divergence: the relay would still produce IQ, it
  // just would not be the y = Hx the topology described.
  const ResolvedTopology resolved = resolve_topology(config_);

  std::unordered_map<std::string, std::size_t> port_by_id;
  port_by_id.reserve(ports.size());
  for (std::size_t p = 0; p != ports.size(); ++p) {
    port_by_id.emplace(ports[p]->config->id, p);
    // -1 marks a role this port does not fill. The thread launch reads it: no
    // puller for a port that is no TX port, no REP worker for one that is no RX
    // port -- a REP worker on a port the producer never writes would block on
    // an empty ring forever.
    ports[p]->tx_port = -1;
    ports[p]->rx_port = -1;
  }
  const auto resolve_port = [&](const std::string& id) {
    auto it = port_by_id.find(id);
    if (it == port_by_id.end()) {
      throw std::runtime_error("radio node references an unknown device: " + id);
    }
    return it->second;
  };

  std::vector<RadioNodeRuntime> nodes(resolved.nodes.size());
  std::deque<RingProgress> node_progress(nodes.size());
  std::unordered_map<std::string, std::size_t> node_by_id;
  node_by_id.reserve(nodes.size());
  for (std::size_t n = 0; n != resolved.nodes.size(); ++n) {
    const auto& declared = resolved.nodes[n];
    auto& node = nodes[n];
    node.id = declared.id;
    node.implicit = declared.implicit;
    node.sample_rate_hz = declared.sample_rate_hz;
    node.rx_model = declared.rx_model.empty() ? nullptr : find_model(config_, declared.rx_model);
    for (std::size_t t = 0; t != declared.tx_ports.size(); ++t) {
      const std::size_t p = resolve_port(declared.tx_ports[t]);
      node.tx_ports.push_back(p);
      ports[p]->node_index = n;
      ports[p]->tx_port = static_cast<int>(t);
    }
    for (std::size_t r = 0; r != declared.rx_ports.size(); ++r) {
      const std::size_t p = resolve_port(declared.rx_ports[r]);
      node.rx_ports.push_back(p);
      ports[p]->node_index = n;
      ports[p]->rx_port = static_cast<int>(r);
    }
    const std::size_t representative =
        node.tx_ports.empty() ? node.rx_ports.front() : node.tx_ports.front();
    node.config = ports[representative]->config;
    node.batch = ports[representative]->batch;
    node_by_id.emplace(node.id, n);


  }

  // Publish the resolved membership and ordering so the canonical matrix index
  // of every port is on the record at startup. Port order is read from the
  // resolved table, never inferred by parsing a numeric suffix off an id.
  for (const auto& node : nodes) {
    std::string line = "event=radio_node_resolved id=" + node.id;
    for (std::size_t t = 0; t != node.tx_ports.size(); ++t) {
      line += " tx[" + std::to_string(t) + "]=" + ports[node.tx_ports[t]]->config->id;
    }
    for (std::size_t r = 0; r != node.rx_ports.size(); ++r) {
      line += " rx[" + std::to_string(r) + "]=" + ports[node.rx_ports[r]]->config->id;
    }
    line += node.implicit ? " implicit=true" : " implicit=false";
    line += "\n";
    std::cout << line;
  }
  std::cout.flush();

  // One LinkRuntime per resolved lane, in the resolved order -- grouped by
  // destination node then rx_port, so a node's incoming list is already
  // row-ordered.
  std::vector<LinkRuntime> links(resolved.lanes.size());
  for (std::size_t i = 0; i != resolved.lanes.size(); ++i) {
    const auto& lane = resolved.lanes[i];
    const auto* model = find_model(config_, lane.model_id);
    if (model == nullptr) {
      throw std::runtime_error("lane model does not exist: " + lane.key);
    }
    links[i].src_node = node_by_id.at(lane.src_node);
    links[i].dst_node = node_by_id.at(lane.dst_node);
    links[i].src_index = resolve_port(lane.src_device);
    links[i].dst_index = resolve_port(lane.dst_device);
    links[i].tx_port = lane.tx_port;
    links[i].rx_port = lane.rx_port;
    links[i].model = model;
    links[i].key = lane.key;
    links[i].physical_key = lane.physical_link_key;
  }

  // Per-node incoming lane lists, resolved once so no serve walks every link.
  for (std::size_t i = 0; i != links.size(); ++i) {
    nodes[links[i].dst_node].incoming.push_back(i);
  }

  // Wake each dependent worker once, even when several matrix lanes share
  // the same source ring or destination node.
  std::vector<std::vector<std::size_t>> port_consumers(ports.size());
  std::vector<std::vector<std::size_t>> node_sources(nodes.size());
  for (const auto& link : links) {
    auto& consumers = port_consumers[link.src_index];
    if (std::find(consumers.begin(), consumers.end(), link.dst_node) == consumers.end()) {
      consumers.push_back(link.dst_node);
    }
    auto& sources = node_sources[link.dst_node];
    if (std::find(sources.begin(), sources.end(), link.src_index) == sources.end()) {
      sources.push_back(link.src_index);
    }
  }

  // Resolve each destination node to the unique physical-link controls that
  // feed it. Multi-port lanes share one control block and therefore receive
  // one receiver-scoped timing update per completed superposition.
  const auto control_links = collect_control_links();
  std::vector<std::vector<BrokerLinkControl*>> node_controls(nodes.size());
  for (const auto& link : links) {
    const auto ctl_it = control_links.find(link.physical_key);
    if (ctl_it == control_links.end() || ctl_it->second == nullptr) {
      continue;
    }
    auto& controls = node_controls[link.dst_node];
    if (std::find(controls.begin(), controls.end(), ctl_it->second) == controls.end()) {
      controls.push_back(ctl_it->second);
    }
  }

  // Live per-worker diagnostics: one entry per port for the puller role, one
  // per node for the serving role. Sized once up front and never resized, so
  // the worker threads and the heartbeat can reference stable elements without
  // synchronisation. (In M0 the two are the same length -- node index equals
  // port index -- but they are indexed distinctly so M0.4 can split them.)
  std::vector<WorkerDiag> puller_diag(ports.size());
  std::vector<WorkerDiag> producer_diag(nodes.size());
  std::vector<WorkerDiag> rep_diag(ports.size());
  std::vector<HopTrace> puller_trace(ports.size());
  std::vector<HopTrace> producer_trace(nodes.size());
  std::vector<HopTrace> rep_trace(ports.size());
  for (auto* traces : {&puller_trace, &producer_trace, &rep_trace}) {
    for (auto& trace : *traces) {
      trace.reserve(!knobs.trace_dir.empty());
    }
  }
  std::vector<ReceiverTimingAccumulator> slot_timing;
  slot_timing.reserve(nodes.size());
  for (const auto& node : nodes) {
    slot_timing.emplace_back(node.batch, node.sample_rate_hz);
  }

  // Bounded stall detector. A producer that cannot make progress emits one
  // diagnostic line per interval naming the phase it is stuck in and the live
  // RX ring occupancies, then a cleared line when it resumes. Deliberately not
  // a fault state machine: a dead RX peer wedges this node exactly the way a
  // dead peer wedged the pre-MIMO server on wait_req, and inventing recovery
  // semantics for it is out of M0's scope.
  constexpr auto kStallReportInterval = std::chrono::milliseconds(2000);
  const auto report_node_stall = [&ports](const RadioNodeRuntime& node, const char* phase,
                                          std::chrono::steady_clock::duration waited) {
    std::string rings;
    for (std::size_t r = 0; r != node.rx_ports.size(); ++r) {
      PortRuntime& out = *ports[node.rx_ports[r]];
      std::size_t size = 0;
      std::size_t cap = 0;
      {
        std::lock_guard<std::mutex> lk(out.rx_mutex);
        size = out.rx_ring.size();
        cap = out.rx_ring.capacity();
      }
      if (r != 0) {
        rings += ",";
      }
      rings += std::to_string(size) + "/" + std::to_string(cap);
    }
    // Composed into one string so concurrent producers cannot interleave
    // fragments of each other's line.
    const std::string line =
        "event=node_stall node=" + node.id + " phase=" + phase + " waited_ms=" +
        std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(waited).count()) +
        " rx_ring=[" + rings + "]\n";
    std::cout << line;
    std::cout.flush();
  };
  const auto report_node_stall_cleared = [](const RadioNodeRuntime& node, const char* phase,
                                            std::chrono::steady_clock::duration waited) {
    const std::string line =
        "event=node_stall_cleared node=" + node.id + " phase=" + phase + " waited_ms=" +
        std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(waited).count()) + "\n";
    std::cout << line;
    std::cout.flush();
  };

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
    HopTrace& trace = puller_trace[d];
    apply_thread_slack(knobs);
    try {
      PortRuntime& dev = *ports[d];
      // recv_buf must hold the largest single ZMQ payload the peer can send.
      // The ring capacity is that hard upper bound: a larger payload could
      // never be relayed and is rejected by recv_samples_into().
      IqBuffer recv_buf(std::max<std::size_t>(dev.queue_samples, dev.batch));
      bool request_outstanding = false;
      // FEWER_COPIES keeps the received message itself and pushes from it.
      zmq_msg_t held;
      zmq_msg_init(&held);
      const IqSample* pending_data = recv_buf.data();
      std::size_t pending = 0;       // samples pulled but not yet in the ring
      bool pending_counted = false;  // ring-full already counted for this message
      while (!stop_requested.load()) {
        const auto room_ticket = dev.tx_room_changed.ticket();
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
            pushed = dev.tx_ring.push(std::span<const IqSample>(pending_data, pending));
          }
          if (pushed) {
            for (const auto n : port_consumers[d]) {
              node_progress[n].notify();
            }
            trace.add(HopEvent::PullRingPushed, pending);
            // Wire capture, in ring order: this is what the peer's TX actually
            // put on the wire, before anything of ours reads it.
            append_capture(dev.tx_capture, dev.capture_limit, dev.capture_skip,
                           dev.tx_wire_samples, pending_data, pending);
            if (knobs.fewer_copies) {
              zmq_msg_close(&held);
              zmq_msg_init(&held);
            }
            diag.state.store("push");
            diag.last_samples.store(pending);
            diag.total_samples.fetch_add(pending);
            diag.progress.fetch_add(1);
            stats.tx_pulls.fetch_add(1);
            pending = 0;
            pending_counted = false;
          } else {
            diag.state.store("wait_room");
            diag.blocked_iters.fetch_add(1);
            if (!pending_counted) {
              stats.tx_queue_overflows.fetch_add(1);
              diag.overflows.fetch_add(1, std::memory_order_relaxed);
              pending_counted = true;
            }
            wait_for_ring(knobs, dev.tx_room_changed, room_ticket,
                          std::chrono::steady_clock::now());
          }
          continue;
        }

        if (room < dev.batch) {
          diag.state.store("wait_room");
          diag.blocked_iters.fetch_add(1);
          wait_for_ring(knobs, dev.tx_room_changed, room_ticket,
                        std::chrono::steady_clock::now());
          continue;
        }
        if (!request_outstanding) {
          diag.state.store("send_req");
          if (!send_request(dev.tx_req.get())) {
            diag.idle_waits.fetch_add(1);
            continue; // send timed out; retry
          }
          request_outstanding = true;
          trace.add(HopEvent::PullReqSent, 0);
        }
        diag.state.store("recv_reply");
        std::size_t sample_count = 0;
        bool got = false;
        if (knobs.fewer_copies) {
          const auto once = [&](int flags) {
            return recv_samples_msg(dev.tx_req.get(), held, recv_buf.size(), sample_count, flags);
          };
          got = knobs.spin_sockets ? spin_receive([&] { return once(ZMQ_DONTWAIT); }, stop_requested, dev.tx_req.get(),
                                          knobs.spin_budget)
                           : once(0);
          if (got) {
            pending_data = static_cast<const IqSample*>(zmq_msg_data(&held));
          }
        } else {
          const auto once = [&](int flags) {
            return recv_samples_into(dev.tx_req.get(), recv_buf, sample_count, flags);
          };
          got = knobs.spin_sockets ? spin_receive([&] { return once(ZMQ_DONTWAIT); }, stop_requested, dev.tx_req.get(),
                                          knobs.spin_budget)
                           : once(0);
          pending_data = recv_buf.data();
        }
        if (!got) {
          diag.idle_waits.fetch_add(1);
          continue; // receive timed out; the request is still outstanding
        }
        request_outstanding = false;
        trace.add(HopEvent::PullReplyRecv, sample_count);
        pending = sample_count; // hand off to the land-pending branch above
      }
      zmq_msg_close(&held);
    } catch (const std::exception& e) {
      report_thread_error("puller", e);
    }
    diag.state.store("stopped");
  };

  // RadioNode producer thread: the single owner of this node's sample epoch,
  // source cursors, channel call, and throttle.
  //
  // This is the structural change M0 exists for. In the pre-MIMO broker the
  // per-destination server thread selected its own serve window at its own
  // instant, so two threads owning the rows of one radio could pick different
  // counts and drift apart permanently. Here there is exactly one thread per
  // node, it chooses `count` once, calls the channel once, and advances every
  // source cursor by that same count in one loop. A state where sibling RX
  // ports consumed different windows is not reachable, because no second
  // thread exists that could choose a window.
  const auto run_producer = [&](std::size_t n) {
    WorkerDiag& diag = producer_diag[n];
    HopTrace& trace = producer_trace[n];
    apply_thread_slack(knobs);
    try {
      RadioNodeRuntime& node = nodes[n];
      const std::vector<std::size_t>& incoming = node.incoming;

      std::vector<IqBuffer> inputs(incoming.size(), IqBuffer(node.batch));
      // One output row per RX port. M0 always has exactly one.
      std::vector<IqBuffer> row_storage(node.rx_ports.size(), IqBuffer(node.batch));
      std::vector<std::span<IqSample>> rows(node.rx_ports.size());
      std::vector<char> row_in_ring(node.rx_ports.size(), 0);

      // Fixed description of this node's incoming lanes; the `samples` span is
      // repointed at the freshly read ring data each slot. process_superposition
      // shapes every lane through its model and accumulates it into the row its
      // rx_port names = the node's RX signal.
      std::vector<SuperpositionInput> superposition(incoming.size());
      for (std::size_t k = 0; k != incoming.size(); ++k) {
        superposition[k].link_key = links[incoming[k]].key;
        superposition[k].model = links[incoming[k]].model;
        superposition[k].rx_port = links[incoming[k]].rx_port;
        superposition[k].tx_port = links[incoming[k]].tx_port;
      }

      // Optional receiver model (thermal-noise floor) applied once per row.
      const ModelConfig* rx_model = node.rx_model;

      const std::uint64_t rate = std::max<std::uint64_t>(1, node.sample_rate_hz);
      bool epoch_set = false;
      const auto batch_duration = std::chrono::nanoseconds(
          (node.batch * 1000000000ULL) /
          std::max<std::uint64_t>(1, node.sample_rate_hz));
      const auto starvation_deadline = batch_duration * 5;
      // Preserve the original clock while independently bounding release
      // credit to one batch and sustained recovery to the configured rate.
      RealTimePacer pacer(rate, batch_duration, config_.runtime.pacing_recovery_ppm);
      std::cout << "event=pacing_config node=" + node.id +
                       " recovery_ppm=" + std::to_string(config_.runtime.pacing_recovery_ppm) +
                       " burst_budget_ns=" + std::to_string(batch_duration.count()) + "\n";

      while (!stop_requested.load()) {
        // (A) Output room. Every RX row must be able to take at least one
        // sample. Partial progress is deliberate and mirrors the input side:
        // demanding a full batch of headroom would strand the partial chunks a
        // lock-step radio leaves behind, which is the B2.2 dead-lock in a new
        // place.
        diag.state.store("wait_room");
        const auto t_room_start = std::chrono::steady_clock::now();
        std::size_t room = 0;
        bool room_stall_reported = false;
        auto next_room_report = t_room_start + kStallReportInterval;
        while (!stop_requested.load()) {
          const auto ticket = node_progress[n].ticket();
          room = node.batch;
          for (const std::size_t r : node.rx_ports) {
            room = std::min(room, rx_headroom(*ports[r]));
          }
          if (room > 0) {
            break;
          }
          diag.blocked_iters.fetch_add(1);
          const auto now = std::chrono::steady_clock::now();
          if (now >= next_room_report) {
            report_node_stall(node, "output_room", now - t_room_start);
            room_stall_reported = true;
            next_room_report = now + kStallReportInterval;
          }
          wait_for_ring(knobs, node_progress[n], ticket, t_room_start);
        }
        // Only a genuine resume clears; a stop request is a shutdown, not
        // recovery, and must not be logged as one.
        if (room_stall_reported && !stop_requested.load()) {
          report_node_stall_cleared(node, "output_room",
                                    std::chrono::steady_clock::now() - t_room_start);
        }
        if (room == 0) {
          break; // stop requested while waiting for output room
        }
        const auto t_align_start = std::chrono::steady_clock::now();
        const double room_us =
            std::chrono::duration<double, std::micro>(t_align_start - t_room_start).count();

        // (B) First slot: co-initialise every incoming lane's cursor to its
        // source's earliest buffered sample in one pass. No ring discards
        // anything before its first consumer co-initialises, so this is each
        // ring's true start (sequence 0) -- the superposition still sums every
        // lane from a common epoch. Co-initialising to the live frontier
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
        const auto t_data_start = std::chrono::steady_clock::now();
        const double align_us =
            std::chrono::duration<double, std::micro>(t_data_start - t_align_start).count();

        // (C) Common input window: the largest amount simultaneously available
        // on EVERY incoming lane, capped at the node batch and at the output
        // room, instead of blocking until each lane holds a full fixed batch.
        // A fixed-batch serve strands the partial final chunk every lock-step
        // radio leaves on its ring; in a multi-device fan-in/fan-out topology
        // no radio can advance to refill it, so the whole relay dead-locks.
        std::size_t count = 0;
        if (incoming.empty()) {
          count = room; // no lanes -> zero-fill, still paced and bounded
        } else {
          const auto wait_start = std::chrono::steady_clock::now();
          bool starvation_counted = false;
          bool data_stall_reported = false;
          auto next_data_report = wait_start + kStallReportInterval;
          while (!stop_requested.load()) {
            const auto ticket = node_progress[n].ticket();
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
                diag.sequence_gaps.fetch_add(1, std::memory_order_relaxed);
              }
              const std::uint64_t avail = src.tx_ring.next_sequence() - cur;
              common = std::min<std::size_t>(common, static_cast<std::size_t>(avail));
            }
            const auto now = std::chrono::steady_clock::now();
            // Only a node that has already produced can be starved; the first
            // wait is the relay filling, not a real-time miss. The slot counter
            // states that directly -- the throttle bookkeeping this used to
            // read was rebased to zero once a second, so a starvation landing
            // in that one slot went uncounted. Check before accepting ready
            // input too: a late wake must count even if data arrived meanwhile.
            if (diag.progress.load() > 0 && !starvation_counted &&
                now - wait_start > starvation_deadline) {
              stats.rx_starvations.fetch_add(1);
              diag.starvations.fetch_add(1, std::memory_order_relaxed);
              starvation_counted = true;
            }
            if (common > 0) {
              count = std::min(common, room);
              break;
            }
            diag.state.store("wait_data");
            diag.blocked_iters.fetch_add(1);
            if (now >= next_data_report) {
              report_node_stall(node, "input_data", now - wait_start);
              data_stall_reported = true;
              next_data_report = now + kStallReportInterval;
            }
            wait_for_ring(knobs, node_progress[n], ticket, wait_start);
          }
          if (data_stall_reported && !stop_requested.load()) {
            report_node_stall_cleared(node, "input_data",
                                      std::chrono::steady_clock::now() - wait_start);
          }
        }
        if (count == 0) {
          break; // stop requested while waiting for lane data
        }
        trace.add(HopEvent::ProduceDataReady, count);
        const auto t_read_start = std::chrono::steady_clock::now();
        const double data_us =
            std::chrono::duration<double, std::micro>(t_read_start - t_data_start).count();

        // (D) Read the window from every incoming lane. Cursors do NOT move
        // here; the window was sized against each ring's live frontier above
        // and this thread is the only one that advances a cursor, so the ring
        // cannot shift under the read.
        for (std::size_t k = 0; k != incoming.size(); ++k) {
          auto& link = links[incoming[k]];
          PortRuntime& src = *ports[link.src_index];
          const std::span<IqSample> window(inputs[k].data(), count);
          std::span<const IqSample> in_place;
          {
            std::lock_guard<std::mutex> lk(src.ring_mutex);
            // FEWER_COPIES: read the window where the puller put it. Only this
            // thread advances the lane cursor and the puller discards nothing
            // at or after it, so the storage is stable until (F) below.
            if (knobs.fewer_copies) {
              in_place = src.tx_ring.view(link.cursor.load(), count);
            }
            if (in_place.empty() && !src.tx_ring.read(link.cursor.load(), window)) {
              throw std::runtime_error("broker serve window vanished from ring: " + link.key);
            }
          }
          superposition[k].samples =
              in_place.empty() ? std::span<const IqSample>(window.data(), window.size()) : in_place;
        }

        // (E) One channel call per node per slot, producing every row. On the
        // CUDA backend the per-lane shaping, the summation, and the receiver
        // model all run on the GPU. No lock needed: prepare() preallocated this
        // node's processor state, so concurrent calls touch disjoint state.
        // DIRECT_ROWS: the row is the RX ring's own tail. (A) reserved the
        // headroom and this is the ring's only writer, so the span is ours
        // until (H) commits it.
        for (std::size_t r = 0; r != rows.size(); ++r) {
          std::span<IqSample> tail;
          if (knobs.direct_rows) {
            PortRuntime& out = *ports[node.rx_ports[r]];
            std::lock_guard<std::mutex> lk(out.rx_mutex);
            tail = out.rx_ring.reserve(count);
          }
          row_in_ring[r] = !tail.empty();
          rows[r] = row_in_ring[r] ? tail : std::span<IqSample>(row_storage[r].data(), count);
        }
        diag.state.store("process");
        const auto t_process_start = std::chrono::steady_clock::now();
        const double read_us =
            std::chrono::duration<double, std::micro>(t_process_start - t_read_start).count();
        processor_->process_superposition(node.id, superposition, rx_model, node.sample_rate_hz,
                                          std::span<std::span<IqSample>>(rows));
        const auto t_commit_start = std::chrono::steady_clock::now();
        const double process_us =
            std::chrono::duration<double, std::micro>(t_commit_start - t_process_start).count();

        // The backend shapes and sums every incoming lane in one call, so the
        // truthful execution measurement is receiver-scoped. Publish the same
        // completed-call timing on each physical link feeding this node.
        const double slot_deadline_us =
            static_cast<double>(count) * 1'000'000.0 / static_cast<double>(rate);
        auto& timing_stats = slot_timing[n];
        timing_stats.observe(count, process_us);
        for (BrokerLinkControl* ctl : node_controls[n]) {
          TelemetrySnapshot ts = read_telemetry_snapshot(*ctl);
          ts.processed_samples = count;
          ts.sample_rate_hz = rate;
          ts.slot_deadline_us = slot_deadline_us;
          ts.channel_process_us = process_us;
          ts.slot_process_count = timing_stats.calls.count;
          ts.slot_deadline_miss_count = timing_stats.calls.deadline_misses;
          ts.slot_process_max_us = timing_stats.calls.max_us;
          ts.slot_process_p95_us = timing_stats.calls.p95_us;
          ts.slot_process_p99_us = timing_stats.calls.p99_us;
          ts.nominal_slot_samples = timing_stats.nominal_samples();
          ts.nominal_pending_samples = timing_stats.pending_samples;
          ts.nominal_pending_estimated_us = timing_stats.pending_estimated_us;
          ts.nominal_latest_estimated_us = timing_stats.latest_nominal_estimated_us;
          ts.nominal_slot_count = timing_stats.nominal_slots.count;
          ts.nominal_deadline_miss_count = timing_stats.nominal_slots.deadline_misses;
          ts.nominal_process_max_us = timing_stats.nominal_slots.max_us;
          ts.nominal_process_p95_us = timing_stats.nominal_slots.p95_us;
          ts.nominal_process_p99_us = timing_stats.nominal_slots.p99_us;
          ts.fragment_call_count = timing_stats.fragment_calls;
          ts.fragment_sample_count = timing_stats.fragment_samples;
          ts.fragment_min_samples = timing_stats.fragment_min();
          ts.fragment_max_samples = timing_stats.fragment_max_samples;
          ts.fragmented_nominal_slot_count = timing_stats.fragmented_nominal_slots;
          publish_telemetry_snapshot(*ctl, ts);
        }

        // (F) Commit: advance every lane cursor by the SAME count, in one
        // loop, in the one thread that owns them. This is the statement that
        // makes sibling-port alignment a structural invariant.
        for (std::size_t k = 0; k != incoming.size(); ++k) {
          links[incoming[k]].cursor.fetch_add(count);
        }
        for (const auto p : node_sources[n]) {
          ports[p]->tx_room_changed.notify();
        }

        // (G) Retain missed nominal time, but recover through a separately
        // rate-limited schedule. Idle time never becomes unlimited burst
        // credit. Output headroom and aligned input availability still gate
        // every publication; the pacer neither drops nor substitutes IQ.
        const auto due = pacer.charge(std::chrono::steady_clock::now(), count);
        if (config_.runtime.pacing && due > std::chrono::steady_clock::now()) {
          diag.state.store("throttle");
          std::this_thread::sleep_until(due);
        }
        const auto t_push_start = std::chrono::steady_clock::now();
        const double throttle_us =
            std::chrono::duration<double, std::micro>(t_push_start - t_commit_start).count();

        // (H) Publish each row to its RX port's ring. Headroom was reserved in
        // (A) and this is the only producer, so the push cannot fail.
        diag.state.store("push");
        for (std::size_t r = 0; r != node.rx_ports.size(); ++r) {
          PortRuntime& out = *ports[node.rx_ports[r]];
          std::lock_guard<std::mutex> lk(out.rx_mutex);
          if (row_in_ring[r]) {
            out.rx_ring.commit(count);
          } else if (!out.rx_ring.push(std::span<const IqSample>(rows[r].data(), count))) {
            throw std::runtime_error("RX output ring rejected a reserved push: " + out.config->id);
          }
          // Publish the window boundary with the samples, under the same lock,
          // so a queued boundary always has its samples already in the ring.
          out.rx_slots.push_back(count);
          const std::size_t occupancy = out.rx_ring.size();
          if (occupancy > out.rx_peak_occupancy.load(std::memory_order_relaxed)) {
            out.rx_peak_occupancy.store(occupancy, std::memory_order_relaxed);
          }
        }
        for (const auto p : node.rx_ports) {
          ports[p]->rx_data_changed.notify();
        }
        pacer.published(std::chrono::steady_clock::now());
        diag.clock_lag_us.store(std::chrono::duration_cast<std::chrono::microseconds>(pacer.lag()).count(),
                                std::memory_order_relaxed);
        diag.clock_max_lag_us.store(std::chrono::duration_cast<std::chrono::microseconds>(pacer.max_lag()).count(),
                                    std::memory_order_relaxed);
        const double push_us =
            std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t_push_start).count();
        trace.add(HopEvent::ProducePushed, count);

        store_us(diag.stage_us_bits[kProducerStageRoom], room_us);
        store_us(diag.stage_us_bits[kProducerStageAlign], align_us);
        store_us(diag.stage_us_bits[kProducerStageData], data_us);
        store_us(diag.stage_us_bits[kProducerStageRead], read_us);
        store_us(diag.stage_us_bits[kProducerStageProcess], process_us);
        record_process_us(diag, process_us);
        store_us(diag.stage_us_bits[kProducerStageThrottle], throttle_us);
        store_us(diag.stage_us_bits[kProducerStagePush], push_us);
        diag.last_samples.store(count);
        diag.progress.fetch_add(1);
      }
    } catch (const std::exception& e) {
      report_thread_error("producer", e);
    }
    diag.state.store("stopped");
  };

  // PortRepWorker: ZMQ REP sink for one RX port. Its only responsibility is
  // the socket state machine -- receive a request, pop this port's row, reply.
  // No window selection, no cursors, no channel, no throttle, no epoch: those
  // all belong to the node producer. It never zero-fills; an empty ring means
  // waiting, exactly as the pre-MIMO server held a request until real IQ was
  // ready.
  const auto run_rep_worker = [&](std::size_t p) {
    WorkerDiag& diag = rep_diag[p];
    HopTrace& trace = rep_trace[p];
    apply_thread_slack(knobs);
    try {
      PortRuntime& port = *ports[p];
      IqBuffer reply_buf(port.batch);
      while (!stop_requested.load()) {
        diag.state.store("wait_req");
        const auto t_wait_req_start = std::chrono::steady_clock::now();
        std::uint8_t dummy = 0;
        int received = -1;
        if (knobs.spin_sockets) {
          spin_receive(
              [&] {
                received = zmq_recv(port.rx_rep.get(), &dummy, sizeof(dummy), ZMQ_DONTWAIT);
                return received >= 0 || (zmq_errno() != EAGAIN && zmq_errno() != EINTR);
              },
              stop_requested, port.rx_rep.get(), knobs.spin_budget);
          if (received < 0 && zmq_errno() != EAGAIN && zmq_errno() != EINTR && zmq_errno() != EFSM) {
            throw std::runtime_error(std::string("rx request failed: ") + zmq_strerror(zmq_errno()));
          }
        } else {
          received = zmq_recv(port.rx_rep.get(), &dummy, sizeof(dummy), 0);
        }
        if (received < 0) {
          const int err = zmq_errno();
          if (err == EAGAIN || err == EINTR || err == EFSM) {
            diag.idle_waits.fetch_add(1);
            continue;
          }
          throw std::runtime_error(std::string("rx request failed: ") + zmq_strerror(err));
        }
        trace.add(HopEvent::RepReqRecv, 0);
        const auto t_pop_start = std::chrono::steady_clock::now();
        const double wait_req_us =
            std::chrono::duration<double, std::micro>(t_pop_start - t_wait_req_start).count();

        // A request was accepted; it must be answered with real processed IQ.
        diag.state.store("wait_row");
        std::size_t take = 0;
        // FEWER_COPIES: send straight from the ring and release the window
        // only after the send. The producer writes free space only, so the
        // window stays intact while it is not discarded.
        std::span<const IqSample> in_ring;
        while (!stop_requested.load()) {
          const auto ticket = port.rx_data_changed.ticket();
          {
            std::lock_guard<std::mutex> lk(port.rx_mutex);
            if (!port.rx_slots.empty()) {
              // One producer window, whole. Never two, and never part of one:
              // both would size this reply off this thread's arrival time
              // rather than off the window the node published.
              take = port.rx_slots.front();
              if (knobs.fewer_copies) {
                in_ring = port.rx_ring.view(port.rx_cursor, take);
              }
              if (in_ring.empty()) {
                if (!port.rx_ring.read(port.rx_cursor, std::span<IqSample>(reply_buf.data(), take))) {
                  throw std::runtime_error("RX output window vanished from ring: " + port.config->id);
                }
                port.rx_cursor += take;
                port.rx_ring.discard_before(port.rx_cursor);
                port.rx_slots.pop_front();
              }
            }
          }
          if (take > 0) {
            break;
          }
          diag.blocked_iters.fetch_add(1);
          wait_for_ring(knobs, port.rx_data_changed, ticket, t_pop_start);
        }
        if (take == 0) {
          break; // stop requested while waiting for a row
        }
        if (in_ring.empty()) {
          node_progress[port.node_index].notify();
        }
        trace.add(HopEvent::RepRowPopped, take);
        const auto t_send_start = std::chrono::steady_clock::now();
        const double pop_us =
            std::chrono::duration<double, std::micro>(t_send_start - t_pop_start).count();

        // Wire capture, in serve order: this is the processed row as it goes
        // out to the radio, so a checker sees the broker's output rather than
        // its intent.
        const std::span<const IqSample> reply =
            in_ring.empty() ? std::span<const IqSample>(reply_buf.data(), take) : in_ring;
        append_capture(port.rx_capture, port.capture_limit, port.capture_skip,
                       port.rx_wire_samples, reply.data(), take);

        diag.state.store("send");
        while (!stop_requested.load() && !send_samples(port.rx_rep.get(), reply)) {
          // send timed out; retry so the REP socket stays in a valid state
        }
        if (!in_ring.empty()) {
          std::lock_guard<std::mutex> lk(port.rx_mutex);
          port.rx_cursor += take;
          port.rx_ring.discard_before(port.rx_cursor);
          port.rx_slots.pop_front();
        }
        if (!in_ring.empty()) {
          node_progress[port.node_index].notify();
        }
        const double send_us =
            std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t_send_start).count();
        trace.add(HopEvent::RepReplySent, take);
        store_us(diag.stage_us_bits[kRepStageWaitReq], wait_req_us);
        store_us(diag.stage_us_bits[kRepStagePop], pop_us);
        store_us(diag.stage_us_bits[kRepStageSend], send_us);
        diag.last_samples.store(take);
        diag.progress.fetch_add(1);
        stats.rx_requests.fetch_add(1);
      }
    } catch (const std::exception& e) {
      report_thread_error("rep_worker", e);
    }
    diag.state.store("stopped");
  };

  // Thread count is nodes + 2 x ports: a puller and a REP worker per port,
  // plus one producer per node. Under M0's implicit singleton lowering that is
  // one more thread per device than the pre-MIMO broker had, and the producer
  // is thin because a node is its port. The GPU call count is unchanged: still
  // exactly one process_superposition per node per slot.
  std::vector<std::thread> workers;
  workers.reserve(2 * ports.size() + nodes.size());
  for (std::size_t p = 0; p != ports.size(); ++p) {
    if (ports[p]->tx_port >= 0) {
      workers.emplace_back(run_puller, p);
    }
  }
  for (std::size_t n = 0; n != nodes.size(); ++n) {
    workers.emplace_back(run_producer, n);
  }
  for (std::size_t p = 0; p != ports.size(); ++p) {
    if (ports[p]->rx_port >= 0) {
      workers.emplace_back(run_rep_worker, p);
    }
  }

  // Heartbeat: once per second, publish each worker's live state so a wedged
  // relay is diagnosable from the broker log without attaching a debugger.
  // Previous heartbeat's health counters per port, so each line can carry the
  // per-second delta next to the run total. Only the heartbeat thread touches
  // these.
  std::vector<std::uint64_t> hb_last_starvations(ports.size(), 0);
  std::vector<std::uint64_t> hb_last_gaps(ports.size(), 0);
  std::vector<std::uint64_t> hb_last_overflows(ports.size(), 0);
  const auto emit_heartbeat = [&](std::uint64_t elapsed_s) {
    for (std::size_t d = 0; d != ports.size(); ++d) {
      std::size_t ring_size = 0;
      std::size_t ring_cap = 0;
      {
        std::lock_guard<std::mutex> lk(ports[d]->ring_mutex);
        ring_size = ports[d]->tx_ring.size();
        ring_cap = ports[d]->tx_ring.capacity();
      }
      // RX output ring occupancy -- the buffer M0.4 added to the latency path.
      std::size_t rx_size = 0;
      std::size_t rx_cap = 0;
      {
        std::lock_guard<std::mutex> lk(ports[d]->rx_mutex);
        rx_size = ports[d]->rx_ring.size();
        rx_cap = ports[d]->rx_ring.capacity();
      }
      const WorkerDiag& p = puller_diag[d];
      // Producer diagnostics belong to the owning node; the REP worker's are
      // this port's own. One port per node in M0, so this stays one line per
      // device.
      const WorkerDiag& s = producer_diag[ports[d]->node_index];
      const WorkerDiag& r = rep_diag[d];
      std::cout << "event=heartbeat t=" << elapsed_s << " dev=" << ports[d]->config->id << " ring="
                << ring_size << "/" << ring_cap << " rx_ring=" << rx_size << "/" << rx_cap
                << " puller[state=" << p.state.load()
                << " pulls=" << p.progress.load() << " idle=" << p.idle_waits.load()
                << " room_stall=" << p.blocked_iters.load() << " last=" << p.last_samples.load()
                << " acquired=" << p.total_samples.load()
                << "] producer[state=" << s.state.load() << " slots=" << s.progress.load()
                << " stall=" << s.blocked_iters.load() << " last=" << s.last_samples.load()
                << "] rep[state=" << r.state.load() << " replies=" << r.progress.load()
                << " idle=" << r.idle_waits.load() << " row_spin=" << r.blocked_iters.load()
                << " last=" << r.last_samples.load() << "]";
      // Health counters: delta since the previous heartbeat, then the run
      // total. starvations/gaps come from the node's producer (its input
      // side), overflows from this port's puller.
      const std::uint64_t starv = s.starvations.load(std::memory_order_relaxed);
      const std::uint64_t gaps = s.sequence_gaps.load(std::memory_order_relaxed);
      const std::uint64_t ovf = p.overflows.load(std::memory_order_relaxed);
      std::cout << " starvations=" << (starv - hb_last_starvations[d]) << " starvations_total=" << starv
                << " gaps=" << (gaps - hb_last_gaps[d]) << " gaps_total=" << gaps
                << " overflows=" << (ovf - hb_last_overflows[d]) << " overflows_total=" << ovf
                << " clock_lag_us=" << s.clock_lag_us.load(std::memory_order_relaxed)
                << " clock_max_lag_us=" << s.clock_max_lag_us.load(std::memory_order_relaxed) << "\n";
      hb_last_starvations[d] = starv;
      hb_last_gaps[d] = gaps;
      hb_last_overflows[d] = ovf;
    }
    // Channel-processor GPU timings (zero on the CPU backend).
    const ProcessorTimings t = processor_->last_timings();
    std::cout << "event=gpu_timings t=" << elapsed_s << " h2d_us=" << t.h2d_us << " kernel_us=" << t.kernel_us
              << " d2h_us=" << t.d2h_us << "\n";
    // Per-node producer stage timings from its last completed slot. process_us
    // is the WHOLE processor call -- on the CUDA backend its h2d/kernel/d2h
    // subset is reported separately by event=gpu_timings above. room_us and
    // data_us are the two waits the producer can block on (output headroom and
    // a common input window); throttle_us is the real-time pacing sleep;
    // align_us is non-zero only on the first slot.
    for (std::size_t n = 0; n != nodes.size(); ++n) {
      const WorkerDiag& s = producer_diag[n];
      std::cout << "event=cpu_stage_timings t=" << elapsed_s << " node=" << nodes[n].id
                << " room_us=" << load_us(s.stage_us_bits[kProducerStageRoom])
                << " align_us=" << load_us(s.stage_us_bits[kProducerStageAlign])
                << " data_us=" << load_us(s.stage_us_bits[kProducerStageData])
                << " read_us=" << load_us(s.stage_us_bits[kProducerStageRead])
                << " process_us=" << load_us(s.stage_us_bits[kProducerStageProcess])
                << " throttle_us=" << load_us(s.stage_us_bits[kProducerStageThrottle])
                << " push_us=" << load_us(s.stage_us_bits[kProducerStagePush]) << "\n";
    }
    // RX output-ring occupancy translated into the latency it adds. This ring
    // is the only buffer the pre-MIMO broker did not have, so its steady-state
    // occupancy IS the added one-way delay; a round trip pays it twice. peak is
    // sampled by the producer after every push, not by this once-a-second loop.
    for (std::size_t d = 0; d != ports.size(); ++d) {
      const PortRuntime& port = *ports[d];
      std::size_t rx_size = 0;
      std::size_t rx_cap = 0;
      {
        std::lock_guard<std::mutex> lk(ports[d]->rx_mutex);
        rx_size = ports[d]->rx_ring.size();
        rx_cap = ports[d]->rx_ring.capacity();
      }
      const std::size_t peak = port.rx_peak_occupancy.load(std::memory_order_relaxed);
      const double rate = static_cast<double>(std::max<std::uint64_t>(1, port.config->sample_rate_hz));
      std::cout << "event=rx_ring t=" << elapsed_s << " dev=" << port.config->id
                << " occupancy=" << rx_size << " peak=" << peak << " capacity=" << rx_cap
                << " high_water=" << port.rx_high_water
                << " added_one_way_us=" << (static_cast<double>(rx_size) * 1e6 / rate)
                << " peak_one_way_us=" << (static_cast<double>(peak) * 1e6 / rate) << "\n";
    }
    // Per-port REP worker timings from its last completed reply.
    for (std::size_t d = 0; d != ports.size(); ++d) {
      const WorkerDiag& r = rep_diag[d];
      std::cout << "event=rep_stage_timings t=" << elapsed_s << " dev=" << ports[d]->config->id
                << " wait_req_us=" << load_us(r.stage_us_bits[kRepStageWaitReq])
                << " pop_us=" << load_us(r.stage_us_bits[kRepStagePop])
                << " send_us=" << load_us(r.stage_us_bits[kRepStageSend]) << "\n";
    }
    std::cout.flush();
  };

  // Relay wedge detector (OCG_BROKER_WEDGE_TIMEOUT_MS). The sum of every
  // worker's progress counter is the relay's clock; it arms once every puller
  // has pulled (the stagger-started UEs are all up) and trips when that clock
  // has not moved for the timeout. It does not try to say WHO stopped -- the
  // per-worker states in the event line do that -- only that a run with no
  // movement anywhere is void and must not run out its duration in silence.
  const auto relay_progress = [&]() {
    std::uint64_t total = 0;
    for (const auto& d : puller_diag) {
      total += d.progress.load(std::memory_order_relaxed);
    }
    for (const auto& d : producer_diag) {
      total += d.progress.load(std::memory_order_relaxed);
    }
    for (const auto& d : rep_diag) {
      total += d.progress.load(std::memory_order_relaxed);
    }
    return total;
  };
  const auto report_relay_wedged = [&](std::chrono::steady_clock::duration waited) {
    std::ostringstream line;
    line << "event=relay_wedged waited_ms="
         << std::chrono::duration_cast<std::chrono::milliseconds>(waited).count() << " ports=[";
    for (std::size_t d = 0; d != ports.size(); ++d) {
      const WorkerDiag& p = puller_diag[d];
      const WorkerDiag& s = producer_diag[ports[d]->node_index];
      const WorkerDiag& r = rep_diag[d];
      line << (d == 0 ? "" : " ") << ports[d]->config->id << ":puller=" << p.state.load() << "/"
           << p.progress.load() << "/" << p.total_samples.load() << ",producer=" << s.state.load() << "/"
           << s.progress.load() << ",rep=" << r.state.load() << "/" << r.progress.load();
    }
    line << "]\n";
    std::cout << line.str() << std::flush;
  };

  const auto start = std::chrono::steady_clock::now();
  auto next_heartbeat = start + std::chrono::seconds(1);
  bool wedge_armed = false;
  std::uint64_t wedge_last_progress = 0;
  auto wedge_last_change = start;
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
    if (knobs.wedge_timeout.count() > 0) {
      if (!wedge_armed) {
        wedge_armed = true;
        for (std::size_t p = 0; p != ports.size(); ++p) {
          if (ports[p]->tx_port >= 0 && puller_diag[p].progress.load(std::memory_order_relaxed) == 0) {
            wedge_armed = false;
          }
        }
        wedge_last_progress = relay_progress();
        wedge_last_change = now;
      } else {
        const std::uint64_t progress = relay_progress();
        if (progress != wedge_last_progress) {
          wedge_last_progress = progress;
          wedge_last_change = now;
        } else if (now - wedge_last_change >= knobs.wedge_timeout) {
          report_relay_wedged(now - wedge_last_change);
          stats.relay_wedged.store(1);
          // Counted as a transport error too: it is the counter the gate
          // scripts and the strict exit path already fail on, and a relay no
          // radio answers is dead transport whatever the radios' reason.
          stats.zmq_errors.fetch_add(1);
          stop_requested.store(true);
          break;
        }
      }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  stop_requested.store(true);
  for (auto& signal : node_progress) {
    signal.notify();
  }
  for (auto& port : ports) {
    port->tx_room_changed.notify();
    port->rx_data_changed.notify();
  }
  for (auto& worker : workers) {
    worker.join();
  }

  if (!knobs.trace_dir.empty()) {
    const std::string path = knobs.trace_dir + "/hop-trace.csv";
    std::ofstream out(path, std::ios::trunc);
    if (!out) {
      throw std::runtime_error("cannot open hop trace: " + path);
    }
    out << "role,id,event,t_ns,samples\n";
    const auto dump = [&out](const char* role, const std::string& id, const HopTrace& trace) {
      for (const auto& r : trace.records) {
        out << role << ',' << id << ',' << r.event << ',' << r.t_ns << ',' << r.samples << '\n';
      }
    };
    for (std::size_t d = 0; d != ports.size(); ++d) {
      dump("puller", ports[d]->config->id, puller_trace[d]);
      dump("rep", ports[d]->config->id, rep_trace[d]);
    }
    for (std::size_t n = 0; n != nodes.size(); ++n) {
      dump("producer", nodes[n].id, producer_trace[n]);
    }
    std::cout << "event=hop_trace path=\"" << path << "\"\n" << std::flush;
  }

  // Final per-device worker breakdown, so the relay outcome is legible even
  // without the heartbeat trail.
  for (std::size_t d = 0; d != ports.size(); ++d) {
    const WorkerDiag& p = puller_diag[d];
    const WorkerDiag& s = producer_diag[ports[d]->node_index];
    const WorkerDiag& r = rep_diag[d];
    std::cout << "event=worker_summary dev=" << ports[d]->config->id
              << " puller[pulls=" << p.progress.load() << " idle=" << p.idle_waits.load()
              << " room_stall=" << p.blocked_iters.load()
              << " acquired=" << p.total_samples.load() << "]"
              << " producer[slots=" << s.progress.load() << " stall=" << s.blocked_iters.load() << "]"
              << " rep[replies=" << r.progress.load() << " idle=" << r.idle_waits.load()
              << " row_spin=" << r.blocked_iters.load() << "]\n";
  }

  // Whole-run latency distribution for the producer's process stage, one line
  // per node. Unlike event=cpu_stage_timings -- which reports a single slot
  // sampled once a second -- these percentiles cover EVERY slot the run
  // processed, so they can be quoted as percentiles. `n` is the slot count they
  // were computed from; quote it alongside any figure taken from this line.
  // Percentiles are bucket upper edges at 5 us resolution; the maximum is
  // the exact elapsed time already recorded by the receiver accumulator.
  for (std::size_t n = 0; n != nodes.size(); ++n) {
    const WorkerDiag& diag = producer_diag[n];
    const std::uint64_t samples = process_sample_count(diag);
    std::cout << "event=process_latency_summary node=" << nodes[n].id
              << " n=" << samples
              << " p50_us=" << process_percentile_us(diag, 0.50)
              << " p95_us=" << process_percentile_us(diag, 0.95)
              << " p99_us=" << process_percentile_us(diag, 0.99)
              << " p999_us=" << process_percentile_us(diag, 0.999)
              << " max_us=" << slot_timing[n].calls.max_us << '\n';
  }
  std::cout.flush();

  // Wire capture files, written after every worker is joined so the buffers are
  // quiescent. The manifest names the node membership and matrix indices the
  // broker RESOLVED, so the checker can bind a file to a row/column of the
  // declared matrix without re-deriving the mapping from id suffixes.
  if (!capture_.directory.empty() && capture_.samples_per_port > 0) {
    std::ostringstream manifest;
    manifest << "{\n  \"schema\": \"ocudu-wire-capture/v1\",\n"
             << "  \"samples_per_port\": " << capture_.samples_per_port << ",\n"
             << "  \"skip_samples\": " << capture_.skip_samples << ",\n"
             << "  \"sample_format\": \"cf32_interleaved_le\",\n"
             << "  \"ports\": [\n";
    for (std::size_t d = 0; d != ports.size(); ++d) {
      PortRuntime& port = *ports[d];
      const std::string id = port.config->id;
      const auto write_file = [&](const char* suffix, const IqBuffer& samples) {
        const std::string path = capture_.directory + "/" + id + "." + suffix + ".cf32";
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        if (!out) {
          throw std::runtime_error("cannot open wire capture file: " + path);
        }
        out.write(reinterpret_cast<const char*>(samples.data()),
                  static_cast<std::streamsize>(samples.size() * sizeof(IqSample)));
        if (!out) {
          throw std::runtime_error("cannot write wire capture file: " + path);
        }
      };
      write_file("tx_in", port.tx_capture);
      write_file("rx_out", port.rx_capture);
      manifest << "    {\"id\": \"" << id << "\", \"node\": \"" << nodes[port.node_index].id
               << "\", \"tx_port\": " << port.tx_port << ", \"rx_port\": " << port.rx_port
               << ", \"tx_in_samples\": " << port.tx_capture.size()
               << ", \"rx_out_samples\": " << port.rx_capture.size() << "}"
               << (d + 1 == ports.size() ? "\n" : ",\n");
    }
    manifest << "  ]\n}\n";
    const std::string manifest_path = capture_.directory + "/wire-capture.json";
    std::ofstream out(manifest_path, std::ios::trunc);
    if (!out) {
      throw std::runtime_error("cannot open wire capture manifest: " + manifest_path);
    }
    out << manifest.str();
    std::cout << "event=wire_capture dir=\"" << capture_.directory
              << "\" samples_per_port=" << capture_.samples_per_port << "\n"
              << std::flush;
  }

  BrokerStats result;
  result.tx_pulls = stats.tx_pulls.load();
  result.rx_requests = stats.rx_requests.load();
  result.rx_starvations = stats.rx_starvations.load();
  result.tx_queue_overflows = stats.tx_queue_overflows.load();
  result.tx_sequence_gaps = stats.tx_sequence_gaps.load();
  result.zmq_errors = stats.zmq_errors.load();
  result.relay_wedged = stats.relay_wedged.load();
  return result;
}

} // namespace ocg
