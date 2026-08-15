#pragma once
// Absolute sample time of a physical link (M2.3).
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

#include <cstdint>

namespace ocg {

struct PhysicalLinkClock {
  // Sample index, counted from the start of the run, of the first sample of
  // the slot about to be processed. Advanced by the slot's sample count once
  // the slot is done.
  std::uint64_t slot_start_samples = 0;
};

} // namespace ocg
