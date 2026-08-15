#pragma once
// State owned by a physical link: its absolute time (M2.3) and its lanes'
// fading grids (M3.3).
//
// Both are here for the same reason. A physical link's lanes are realisations
// of ONE channel: they share a time origin, and from M3 they also share a
// correlation structure. State that a single lane could advance or redraw on
// its own is state that can drift out of that shared reality, so it lives once
// per link and lanes only reference it.
//
// A link's stochastic channel evolves in time, and every lane of that link is
// a realisation of the SAME channel at the SAME instant: the matrix H(t) in
// y = H(t)x is one object with one time origin. Two lanes reading different
// instants would still produce plausible IQ, and the relay would keep running,
// but the output would no longer be the y = Hx the topology describes.
//
// Before M2.3 each lane carried its own `slot_start_samples` accumulator and
// advanced it itself. They stayed in step, but only because the node producer
// happens to hand every lane the same sample count each slot -- a side effect
// of today's broker, not an invariant of the model. This type removes the
// possibility instead of relying on the coincidence: the counter lives once per
// physical link, lanes read it, and nothing per-lane can advance it. It is the
// same move M0 made for cursors, applied to time.
//
// Ownership and threading: a physical link has exactly one destination node, so
// all of its lanes are processed by that node's producer thread. One writer per
// clock, no lock needed. The processor that owns the clocks advances each
// touched clock exactly once per slot, after the slot's lanes have been shaped.

#include "ocudu_gpu_channel/delay.h"
#include <cstdint>
#include <vector>

namespace ocg {

struct PhysicalLinkClock {
  // Sample index, counted from the start of the run, of the first sample of
  // the slot about to be processed. Advanced by the slot's sample count once
  // the slot is done.
  std::uint64_t slot_start_samples = 0;
};

// This slot's Jakes grids for every lane of one physical link (M3.3).
//
// Indexed [lane][chain step], lane order l = r*Nt + t. Filled by a pass that
// runs before any lane of the slot is shaped, so that M3.4 can apply the
// cross-lane mixing in between -- correlation needs every lane's grid at once,
// which is impossible while a lane builds its grid inside its own convolution.
//
// Written by the one producer thread that owns the link's destination node, so
// no locking: a physical link has exactly one destination.
struct PhysicalLinkFading {
  std::vector<std::vector<FadingGrid>> lane_grids;

  // Sizes the table for a link of `lane_count` lanes whose model chain has
  // `steps` steps. Idempotent, and cheap after the first slot: the grids keep
  // their storage across slots and are overwritten in place.
  void reshape(std::size_t lane_count, std::size_t steps)
  {
    if (lane_grids.size() < lane_count) {
      lane_grids.resize(lane_count);
    }
    for (auto& per_step : lane_grids) {
      if (per_step.size() < steps) {
        per_step.resize(steps);
      }
    }
  }
};

} // namespace ocg
