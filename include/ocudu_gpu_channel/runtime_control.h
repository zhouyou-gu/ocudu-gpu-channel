#pragma once
// Runtime-mutable channel parameters — host-side shadow buffer and
// snap-at-slot-boundary helper.
//
// v1 (landed): per-link scalar shadow + atomic seqno + snap helper. See
//   docs/plans/runtime-mutable-channel.md.
// v2 (in-progress): adds ProfileShadow (multi-tap), take_effect_at_slot
//   deterministic timing, and per-link current_slot counter. See
//   docs/plans/runtime-mutable-channel-v2.md.
//
// Concurrency model (unchanged across v1 → v2):
//   - The ZMQ control plane runs on a dedicated thread.
//     It is the *only* writer to each link's BrokerLinkControl shadow
//     fields (scalars, profile, take_effect_at_slot).
//   - Each per-link server thread is the only reader; it observes a snap
//     point at the start of every serve, copies shadow → live if seqno
//     advanced (and the take_effect_at_slot gate has elapsed), and treats
//     `live` as the canonical params for that slot.
//   - Pairing: the control thread writes shadow then bumps seqno with
//     memory_order_release; the server thread loads seqno with
//     memory_order_acquire and only then reads shadow. This ordering
//     guarantees the server observes a fully-written shadow whenever it
//     observes the seqno bump.
//
// When no control plane is wired, snap is a single relaxed load +
// branch-not-taken. Cost is negligible.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/device_channel.h"
#include "ocudu_gpu_channel/mutable_params.h"

#include <atomic>
#include <cstdint>

namespace ocg {

// v2 ProfileShadow — the multi-tap payload a `profile_swap` REQ writes.
// Stored max-sized (kDeviceMaxTaps) so the shadow → live copy is a single
// POD memcpy, no heap allocation in the hot path. Only the first
// `n_taps` entries of `taps[]` are meaningful; the rest are zero-padded.
struct ProfileShadow {
  int        n_taps = 0;
  TapSpec    taps[kDeviceMaxTaps]{};
  bool       fading_enabled = false;
  float      fading_f_d_max_hz = 0.0F;
  int        fading_spectrum = 0;            // 0=Jakes (only one implemented)
  float      fading_grid_us = 100.0F;
};

// One BrokerLinkControl per emulator link. The shadow buffer is initialised
// in prepare() to mirror the per-link YAML state; the seqno starts at 0 so
// the first server-thread snap on the first slot is a no-op (live already
// matches shadow, both already match YAML).
struct BrokerLinkControl {
  // v1 scalar shadow (unchanged accessor surface for back-compat).
  MutableParams              shadow;

  // v2 profile-swap shadow. `profile_pending` is set by the control
  // thread when shadow_profile holds a new profile to apply; cleared by
  // the snap path after the refresh runs. Both writes are paired with
  // seqno's release-store.
  ProfileShadow              shadow_profile;
  bool                       profile_pending = false;

  // v2 deterministic-timing knob. 0 = "apply at next slot boundary"
  // (v1 semantics, preserved when omitted from the REQ).
  std::uint64_t              take_effect_at_slot = 0;

  // v2 per-link slot counter, written by the snap path before each gate
  // check; read by the control thread to know whether scheduled-in-past
  // updates apply now.
  std::atomic<std::uint64_t> current_slot{0};

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
//
// v2: also zeros the profile-swap fields. `shadow_profile` is left default-
// constructed; `profile_pending` and `take_effect_at_slot` are explicitly 0
// so the snap path's gate checks default to "no profile pending, apply
// scalar updates ASAP" (= v1 behaviour).
inline void init_broker_link_control(BrokerLinkControl& ctl,
                                     const MutableParams& yaml_initial)
{
  ctl.shadow = yaml_initial;
  ctl.shadow_profile = ProfileShadow{};
  ctl.profile_pending = false;
  ctl.take_effect_at_slot = 0;
  ctl.current_slot.store(0, std::memory_order_relaxed);
  ctl.seqno.store(0, std::memory_order_relaxed);
}

}  // namespace ocg
