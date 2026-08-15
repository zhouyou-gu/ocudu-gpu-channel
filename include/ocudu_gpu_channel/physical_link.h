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
#include <algorithm>
#include <complex>
#include <cstdint>
#include <stdexcept>
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

  // M3.4: the lane mixing matrix L, row-major, lanes x lanes, with L L^H = R.
  // EMPTY for an iid link, and an empty matrix means the mixing step is skipped
  // outright rather than multiplied by the identity -- multiplying would change
  // the order of float operations and cost the bit-exact evidence that an
  // uncorrelated topology still behaves exactly as it did before M3.
  std::vector<std::complex<float>> mixing;
  int lanes = 1;
  // Which lanes have had their grid built for the slot in progress. Mixing
  // reads every lane of the link at once, so it has to know they are all there:
  // a stale row would be silently mixed into the others.
  std::vector<char> lane_ready;

  void begin_slot()
  {
    lane_ready.assign(static_cast<std::size_t>(std::max(lanes, 1)), 0);
  }

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
    if (lane_ready.size() < lane_count) {
      lane_ready.resize(lane_count, 0);
    }
  }

  // Replaces the link's independent grids w with the correlated g = L w, at
  // every tap and every grid point of the chain-leading step.
  //
  // Only the leading step is mixed. It is the propagation step -- the channel
  // itself -- and the validator requires a correlated model to lead with a
  // fading tdl, so "the correlated step" and "step 0" are the same thing by
  // construction rather than by convention.
  void apply_mixing()
  {
    if (mixing.empty()) {
      return; // iid: untouched, so bit-identical to the pre-M3 path
    }
    const auto n = static_cast<std::size_t>(lanes);
    for (std::size_t l = 0; l != n; ++l) {
      if (l >= lane_ready.size() || lane_ready[l] == 0) {
        throw std::runtime_error(
            "spatial correlation needs every lane of the link in the same slot, "
            "but one lane's grid was not built");
      }
    }
    const FadingGrid& first = lane_grids[0][0];
    const std::size_t taps = first.taps;
    const std::size_t points = first.points;
    if (taps == 0 || points == 0) {
      return;
    }
    if (scratch.size() < n) {
      scratch.resize(n);
    }
    for (std::size_t k = 0; k != taps; ++k) {
      for (std::size_t g = 0; g != points; ++g) {
        for (std::size_t l = 0; l != n; ++l) {
          scratch[l] = lane_grids[l][0].tap(k)[g];
        }
        for (std::size_t l = 0; l != n; ++l) {
          std::complex<float> acc{0.0F, 0.0F};
          const std::complex<float>* row = mixing.data() + l * n;
          for (std::size_t c = 0; c != n; ++c) {
            acc += row[c] * scratch[c];
          }
          lane_grids[l][0].tap(k)[g] = acc;
        }
      }
    }
  }

private:
  std::vector<std::complex<float>> scratch;
};

} // namespace ocg
