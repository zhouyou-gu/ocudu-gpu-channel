#pragma once
// Runtime-mutable channel parameters — host-side shadow buffer and
// snap-at-slot-boundary helper (Phase 3 v1).
//
// Concurrency model:
//   - The ZMQ control plane runs on a dedicated thread (Phase 3 C3+).
//     It is the *only* writer to each link's BrokerLinkControl::shadow.
//   - Each per-link server thread is the only reader; it observes a snap
//     point at the start of every serve, copies shadow → live if seqno
//     advanced, and treats `live` as the canonical params for that slot.
//   - Pairing: the control thread writes shadow then bumps seqno with
//     memory_order_release; the server thread loads seqno with
//     memory_order_acquire and only then reads shadow. This ordering
//     guarantees the server observes a fully-written shadow whenever it
//     observes the seqno bump.
//
// In v1 the control plane is absent and seqno is never bumped, so every
// snap call is a single relaxed load + branch-not-taken. Cost is negligible.
// See docs/plans/runtime-mutable-channel.md for the full design.

#include "ocudu_gpu_channel/mutable_params.h"

#include <atomic>
#include <cstdint>

namespace ocg {

// One BrokerLinkControl per emulator link. The shadow buffer is initialised
// in prepare() to mirror the per-link YAML state; the seqno starts at 0 so
// the first server-thread snap on the first slot is a no-op (live already
// matches shadow, both already match YAML).
struct BrokerLinkControl {
  MutableParams              shadow;          // written by control thread only
  std::atomic<std::uint32_t> seqno{0};        // bumped after every shadow write

  BrokerLinkControl() = default;

  // Non-copyable / non-movable: BrokerLinkControl is pinned by reference
  // for the life of the broker. std::atomic is non-copyable; we also do not
  // want callers to accidentally lose the seqno relationship by moving.
  BrokerLinkControl(const BrokerLinkControl&) = delete;
  BrokerLinkControl(BrokerLinkControl&&) = delete;
  BrokerLinkControl& operator=(const BrokerLinkControl&) = delete;
  BrokerLinkControl& operator=(BrokerLinkControl&&) = delete;
};

// Snap the per-link mutable params from shadow into live, if the control
// thread has bumped seqno since the last snap. Called once per slot per link
// from each backend's hot path, immediately before the chain executes (CPU)
// or build_steps generates the per-slot GpuStep array (CUDA).
//
// Returns true if `live` was updated this call (caller may want to log it
// or trigger downstream recomputation of derived fields, e.g. tap-0 fields
// once tap-0 mutability lands in a follow-on commit).
inline bool snap_mutable_params(MutableParams& live,
                                std::uint32_t& live_seqno,
                                BrokerLinkControl& ctl)
{
  // Acquire-load pairs with the control thread's release-store on bump.
  const std::uint32_t observed = ctl.seqno.load(std::memory_order_acquire);
  if (observed == live_seqno) {
    return false;
  }
  live = ctl.shadow;
  live_seqno = observed;
  return true;
}

// Initialise BrokerLinkControl::shadow from the YAML-derived MutableParams.
// Called once per link in each backend's prepare(). After this call, the
// first slot's snap is a no-op (seqno still 0, live already == shadow).
inline void init_broker_link_control(BrokerLinkControl& ctl,
                                     const MutableParams& yaml_initial)
{
  ctl.shadow = yaml_initial;
  ctl.seqno.store(0, std::memory_order_relaxed);
}

}  // namespace ocg
