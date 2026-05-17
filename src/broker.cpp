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
#include <thread>
#include <vector>
#include <zmq.h>

// Concurrent per-direction relay broker, modelled on srsRAN's GNU Radio
// Companion ZMQ broker (ZMQ REQ source -> Throttle -> channel -> ZMQ REP sink).
// Each device gets a dedicated puller thread (ZMQ REQ, drains the peer's TX
// into a ring) and a dedicated server thread (ZMQ REP, feeds the peer's RX
// from the processed link). The server is REQ/REP-gated and never zero-fills:
// it holds a request until real processed IQ is ready, and throttles its serve
// to the device sample rate so both directions stay balanced and lock-step.
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

bool send_samples(void* socket, const IqBuffer& samples)
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

// One device's broker-side state: a REQ socket draining the device TX into a
// ring, and a REP socket feeding the device RX.
struct Device {
  const DeviceConfig* config = nullptr;
  SocketPtr tx_req;
  SocketPtr rx_rep;
  IqRing tx_ring;
  std::mutex ring_mutex;
  std::size_t batch = 0;
};

// One link's runtime state. The cursor is advanced by the destination device's
// server thread and read by the source device's puller thread, so it is atomic.
struct LinkRuntime {
  std::size_t src_index = 0;
  std::size_t dst_index = 0;
  const ModelConfig* model = nullptr;
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
};

} // namespace

Broker::Broker(TopologyConfig config) : config_(std::move(config))
{
  auto errors = validate_config(config_);
  if (!errors.empty()) {
    throw std::runtime_error("invalid topology: " + errors.front());
  }
  processor_ = create_channel_processor(config_);
}

BrokerStats Broker::run(std::chrono::milliseconds duration)
{
  stop_requested.store(false);
  std::signal(SIGINT, signal_handler);
  std::signal(SIGTERM, signal_handler);

  ContextPtr context(zmq_ctx_new());
  if (context == nullptr) {
    throw std::runtime_error(std::string("zmq_ctx_new failed: ") + zmq_strerror(zmq_errno()));
  }

  AtomicStats stats;

  // Build per-device broker state.
  std::vector<std::unique_ptr<Device>> devices;
  devices.reserve(config_.devices.size());
  for (const auto& device : config_.devices) {
    auto dev = std::make_unique<Device>();
    dev->config = &device;
    dev->batch = resolve_batch_samples(config_.runtime, device.sample_rate_hz);
    dev->tx_ring.reset(config_.runtime.queue_samples);
    dev->tx_req = make_socket(context.get(), ZMQ_REQ);
    dev->rx_rep = make_socket(context.get(), ZMQ_REP);
    if (zmq_connect(dev->tx_req.get(), device.tx_endpoint.c_str()) != 0) {
      throw std::runtime_error("failed to connect TX REQ for " + device.id + ": " + zmq_strerror(zmq_errno()));
    }
    if (zmq_bind(dev->rx_rep.get(), device.rx_endpoint.c_str()) != 0) {
      throw std::runtime_error("failed to bind RX REP for " + device.id + ": " + zmq_strerror(zmq_errno()));
    }
    std::cout << "event=socket_ready device=" << device.id << " tx_connect=" << device.tx_endpoint
              << " rx_bind=" << device.rx_endpoint << "\n";
    devices.push_back(std::move(dev));
  }

  // Build per-link runtime state.
  std::vector<LinkRuntime> links(config_.links.size());
  for (std::size_t i = 0; i != config_.links.size(); ++i) {
    const auto& link = config_.links[i];
    const auto* src = find_device(config_, link.from);
    const auto* dst = find_device(config_, link.to);
    const auto* model = find_model(config_, link.model);
    if (src == nullptr || dst == nullptr || model == nullptr) {
      throw std::runtime_error("invalid link: " + link_key(link));
    }
    for (std::size_t d = 0; d != config_.devices.size(); ++d) {
      if (config_.devices[d].id == link.from) {
        links[i].src_index = d;
      }
      if (config_.devices[d].id == link.to) {
        links[i].dst_index = d;
      }
    }
    links[i].model = model;
  }

  // Live per-worker diagnostics, one entry per device for each thread role.
  // Sized once up front and never resized, so the worker threads and the
  // heartbeat can reference stable elements without synchronisation.
  std::vector<WorkerDiag> puller_diag(devices.size());
  std::vector<WorkerDiag> server_diag(devices.size());

  const auto report_thread_error = [&stats](const char* role, const std::exception& e) {
    stats.zmq_errors.fetch_add(1);
    stop_requested.store(true);
    std::cerr << "event=error role=" << role << " detail=\"" << e.what() << "\"\n";
  };

  // Puller thread: ZMQ REQ source. Drains device d's TX flat-out (bounded only
  // by ring room), exactly as srsRAN's RX channel drains a peer TX.
  const auto run_puller = [&](std::size_t d) {
    WorkerDiag& diag = puller_diag[d];
    try {
      Device& dev = *devices[d];
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

  // Server thread: ZMQ REP sink. Feeds device d's RX with processed IQ, one
  // request at a time, holding the request until real data is ready, and
  // throttling the serve to the device sample rate.
  const auto run_server = [&](std::size_t d) {
    WorkerDiag& diag = server_diag[d];
    try {
      Device& dev = *devices[d];
      std::vector<std::size_t> incoming;
      for (std::size_t i = 0; i != links.size(); ++i) {
        if (links[i].dst_index == d) {
          incoming.push_back(i);
        }
      }
      IqBuffer rx_reply(dev.batch);
      std::vector<IqBuffer> inputs(incoming.size(), IqBuffer(dev.batch));
      std::vector<IqBuffer> outputs(incoming.size(), IqBuffer(dev.batch));

      const std::uint64_t rate = std::max<std::uint64_t>(1, dev.config->sample_rate_hz);
      std::chrono::steady_clock::time_point throttle_anchor{};
      std::uint64_t served = 0;
      bool throttle_anchored = false;
      const auto batch_duration =
          std::chrono::nanoseconds((dev.batch * 1000000000ULL) / std::max<std::uint64_t>(1, dev.config->sample_rate_hz));
      const auto starvation_deadline = batch_duration * 5;

      while (!stop_requested.load()) {
        diag.state.store("wait_req");
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

        // A request was accepted; it must be answered with real processed IQ.
        std::fill(rx_reply.begin(), rx_reply.end(), IqSample{});
        bool aborted = false;
        for (std::size_t k = 0; k != incoming.size(); ++k) {
          auto& link = links[incoming[k]];
          Device& src = *devices[link.src_index];
          bool got = false;
          const auto wait_start = std::chrono::steady_clock::now();
          bool starvation_counted = false;
          while (!stop_requested.load() && !got) {
            {
              std::lock_guard<std::mutex> lk(src.ring_mutex);
              if (!link.cursor_init.load()) {
                link.cursor.store(src.tx_ring.earliest_sequence());
                link.cursor_init.store(true);
              }
              std::uint64_t cur = link.cursor.load();
              if (cur < src.tx_ring.earliest_sequence()) {
                cur = src.tx_ring.earliest_sequence();
                link.cursor.store(cur);
                stats.tx_sequence_gaps.fetch_add(1);
              }
              if (src.tx_ring.read(cur, inputs[k])) {
                got = true;
              }
            }
            if (!got) {
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
          if (!got) {
            aborted = true; // stopping
            break;
          }
          diag.state.store("process");
          // No lock needed: prepare() preallocated every link's processor
          // state, so concurrent process_into() calls touch disjoint state.
          processor_->process_into(link_key(config_.links[incoming[k]]), *link.model, inputs[k], outputs[k],
                                   dev.config->sample_rate_hz);
          for (std::size_t s = 0; s != rx_reply.size(); ++s) {
            rx_reply[s] += outputs[k][s];
          }
          link.cursor.fetch_add(inputs[k].size());
        }
        if (aborted) {
          break;
        }

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

        diag.state.store("send");
        while (!stop_requested.load() && !send_samples(dev.rx_rep.get(), rx_reply)) {
          // send timed out; retry so the REP socket stays in a valid state
        }
        served += rx_reply.size();
        diag.last_samples.store(rx_reply.size());
        diag.progress.fetch_add(1);
        stats.rx_requests.fetch_add(1);
      }
    } catch (const std::exception& e) {
      report_thread_error("server", e);
    }
    diag.state.store("stopped");
  };

  std::vector<std::thread> workers;
  workers.reserve(devices.size() * 2);
  for (std::size_t d = 0; d != devices.size(); ++d) {
    workers.emplace_back(run_puller, d);
    workers.emplace_back(run_server, d);
  }

  // Heartbeat: once per second, publish each worker's live state so a wedged
  // relay is diagnosable from the broker log without attaching a debugger.
  const auto emit_heartbeat = [&](std::uint64_t elapsed_s) {
    for (std::size_t d = 0; d != devices.size(); ++d) {
      std::size_t ring_size = 0;
      std::size_t ring_cap = 0;
      {
        std::lock_guard<std::mutex> lk(devices[d]->ring_mutex);
        ring_size = devices[d]->tx_ring.size();
        ring_cap = devices[d]->tx_ring.capacity();
      }
      const WorkerDiag& p = puller_diag[d];
      const WorkerDiag& s = server_diag[d];
      std::cout << "event=heartbeat t=" << elapsed_s << " dev=" << devices[d]->config->id << " ring="
                << ring_size << "/" << ring_cap << " puller[state=" << p.state.load()
                << " pulls=" << p.progress.load() << " idle=" << p.idle_waits.load()
                << " room_stall=" << p.blocked_iters.load() << " last=" << p.last_samples.load()
                << "] server[state=" << s.state.load() << " serves=" << s.progress.load()
                << " idle=" << s.idle_waits.load() << " data_spin=" << s.blocked_iters.load()
                << " last=" << s.last_samples.load() << "]\n";
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
  for (std::size_t d = 0; d != devices.size(); ++d) {
    const WorkerDiag& p = puller_diag[d];
    const WorkerDiag& s = server_diag[d];
    std::cout << "event=worker_summary dev=" << devices[d]->config->id
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
