#pragma once
// ZMQ REP control plane for runtime-mutable channel parameters (Phase 3 C3).
//
// External clients send a JSON REQ on the configured endpoint, the server
// validates the requested param against a whitelist + range, writes the new
// value into the corresponding BrokerLinkControl::shadow, bumps seqno, and
// replies with a JSON REP. The per-link server thread observes the seqno
// advance at the next slot boundary and snaps shadow → live before the
// kernel runs. See docs/plans/runtime-mutable-channel.md for the full design.
//
// Threading: ControlServer owns a dedicated background thread for the REP
// socket loop; it is the only writer to each BrokerLinkControl::shadow.
// Lookups in `link_map` are read-only after construction so no lock is
// needed inside the loop. The broker's data-plane threads are never blocked
// by control-plane traffic.

#include "ocudu_gpu_channel/runtime_control.h"

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

namespace ocg {

struct ControlServerConfig {
  // ZMQ endpoint to bind the REP socket to. Conventional default:
  // "tcp://*:5559" — see docs/plans/runtime-mutable-channel.md decision #1.
  std::string endpoint;

  // Total receive timeout per loop iteration (milliseconds). Lower values
  // shorten stop() latency; higher values reduce wakeups. Default 100 ms
  // keeps shutdown responsive without burning CPU on empty polls.
  int recv_timeout_ms = 100;

  // Per-message log sink. Default writes one line to std::cout per
  // applied update or rejection in the existing broker's k=v format
  // (event=control_update / event=control_error). Tests override this to
  // capture and assert log content. Pass an empty std::function to
  // suppress logging entirely.
  std::function<void(std::string_view)> logger;
};

class ControlServer {
public:
  // link_map: link_id (e.g. "ue0-gnb0") → non-owning pointer to the
  // BrokerLinkControl that the backend allocated as part of its per-link
  // state. The map must outlive the ControlServer; ownership of the
  // BrokerLinkControl objects stays with whoever allocated them (today:
  // the backend's per-link state structs).
  using LinkMap = std::unordered_map<std::string, BrokerLinkControl*>;

  ControlServer(ControlServerConfig config, LinkMap link_map);
  ~ControlServer();

  // Non-copyable, non-movable: the server pins itself by reference for the
  // life of the broker.
  ControlServer(const ControlServer&) = delete;
  ControlServer(ControlServer&&) = delete;
  ControlServer& operator=(const ControlServer&) = delete;
  ControlServer& operator=(ControlServer&&) = delete;

  // Start the REP socket loop on a background thread. Idempotent — calling
  // start() on an already-started server is a no-op.
  void start();

  // Signal the background thread to stop and join it. Safe to call from
  // any thread; safe to call multiple times. The destructor calls this
  // implicitly so callers do not need to remember.
  void stop();

  // Counters surfaced by event=stats logs (Phase 3 C4 wires the log line).
  struct Stats {
    std::uint64_t msgs_received       = 0;
    std::uint64_t updates_applied     = 0;
    std::uint64_t updates_rejected    = 0;
    std::uint64_t batches_committed   = 0;   // v2.3
    std::uint64_t batches_aborted     = 0;   // v2.3
  };
  Stats stats() const;

  // v2.3 multi-link atomic batches. Public so the cpp-internal free
  // handlers can append to them; control-thread-only access in practice.
  // open_batches_ is keyed by caller-supplied batch_id and lives in the
  // ControlServer instance for the duration between batch_begin and
  // batch_commit/batch_abort.
  struct StagedOp {
    enum class Kind { Scalar, ProfileSwap };
    Kind        kind = Kind::Scalar;
    std::string link_id;
    // Scalar fields (when kind == Scalar)
    std::string param;
    double      value = 0.0;
    // Profile fields (when kind == ProfileSwap)
    ProfileShadow profile;
  };
  struct StagedBatch {
    std::vector<StagedOp> ops;
  };

  // Synchronous message handler — exposed for unit testing. The REP socket
  // loop calls this for every received frame; tests can call it directly to
  // exercise validation + shadow updates without spinning up ZMQ. Returns
  // the JSON REP body that the server would send back to the client.
  //
  // v2: dispatches on the `type` field of the JSON envelope. Defaults to
  // "scalar" for v1 back-compat. Currently recognised types: "scalar",
  // "profile_swap", "batch_begin", "batch_commit", "batch_abort".
  std::string handle_message(const std::string& request_body);

private:
  ControlServerConfig config_;
  LinkMap link_map_;
  std::unordered_map<std::string, StagedBatch> open_batches_;

  std::atomic<bool> running_{false};
  std::atomic<bool> stop_requested_{false};
  std::thread thread_;

  mutable std::atomic<std::uint64_t> msgs_received_{0};
  mutable std::atomic<std::uint64_t> updates_applied_{0};
  mutable std::atomic<std::uint64_t> updates_rejected_{0};
  mutable std::atomic<std::uint64_t> batches_committed_{0};
  mutable std::atomic<std::uint64_t> batches_aborted_{0};

  void run_loop();
};

}  // namespace ocg
