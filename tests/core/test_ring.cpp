#include "ocudu_gpu_channel/ring.h"
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <random>
#include <iostream>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

} // namespace

int main()
{
  ocg::IqRing ring(4);
  ocg::IqBuffer first = {{1.0F, 0.0F}, {2.0F, 0.0F}};
  ocg::IqBuffer second = {{3.0F, 0.0F}, {4.0F, 0.0F}};
  ocg::IqBuffer overflow = {{5.0F, 0.0F}};
  ocg::IqBuffer out(2);

  require(ring.push(first), "first push");
  require(ring.push(second), "second push");
  require(!ring.push(overflow), "bounded ring rejects overflow");
  require(ring.read(0, out), "first cursor read");
  require(out[0].i == 1.0F && out[1].i == 2.0F, "first cursor data");
  require(ring.read(2, out), "second cursor read");
  require(out[0].i == 3.0F && out[1].i == 4.0F, "second cursor data");
  require(!ring.read(3, out), "partial future read rejected");
  ring.discard_before(2);
  require(ring.size() == 2, "discard releases consumed samples");
  require(ring.push(overflow), "push after discard");
  require(ring.next_sequence() == 5, "sequence remains monotonic");

  // True wrap-around: after a discard, start_ > 0; a subsequent push that
  // spans the buffer boundary must wrap correctly. Verifies the (start_ +
  // size_) % capacity modular arithmetic in push/read.
  {
    ocg::IqRing wrap(4);
    ocg::IqBuffer four = {{10.0F, 0.0F}, {20.0F, 0.0F}, {30.0F, 0.0F}, {40.0F, 0.0F}};
    require(wrap.push(four), "wrap: prime ring full");
    wrap.discard_before(2);  // drop seq 0..1; start_ -> 2, size_ -> 2
    require(wrap.size() == 2, "wrap: after discard size halved");
    ocg::IqBuffer two_more = {{50.0F, 0.0F}, {60.0F, 0.0F}};
    require(wrap.push(two_more), "wrap: push spans buffer boundary");
    ocg::IqBuffer rd(4);
    require(wrap.read(2, rd), "wrap: read across the wrap");
    require(rd[0].i == 30.0F && rd[1].i == 40.0F && rd[2].i == 50.0F && rd[3].i == 60.0F,
            "wrap: read returns samples in monotonic sequence across the boundary");
  }

  // reset() must clear sequence + size + start cursor independent of previous
  // capacity. discard_before(seq >= next_sequence) is also asserted here as
  // the full-reset early return path inside discard_before.
  {
    ocg::IqRing r(2);
    ocg::IqBuffer one = {{7.0F, 0.0F}};
    require(r.push(one), "reset: prime");
    r.discard_before(10);  // sequence beyond next_sequence -> full reset
    require(r.size() == 0, "discard_before(>=next_sequence) drains the ring");
    require(r.push(one) && r.push(one), "ring usable after full-drain discard");
    r.reset(8);
    require(r.size() == 0 && r.next_sequence() == 0,
            "reset() returns ring to fresh state with new capacity");
    ocg::IqBuffer eight = {{1.0F, 0.0F}, {2.0F, 0.0F}, {3.0F, 0.0F}, {4.0F, 0.0F},
                           {5.0F, 0.0F}, {6.0F, 0.0F}, {7.0F, 0.0F}, {8.0F, 0.0F}};
    require(r.push(eight), "reset: new capacity honoured");
  }

  // Randomised check against a plain deque model: push/read/discard sequences
  // of every length, over odd capacities, so pushes and reads start and end at
  // every offset relative to the wrap (the block-copy path has two segments,
  // and an off-by-one at the boundary would only show at specific offsets).
  for (std::size_t cap : {1U, 3U, 7U, 16U, 61U}) {
    ocg::IqRing r(cap);
    std::deque<float> model;
    std::uint64_t model_next = 0;
    std::mt19937 rng(static_cast<unsigned>(cap));
    float value = 0.0F;
    for (int op = 0; op != 4000; ++op) {
      const int kind = static_cast<int>(rng() % 3);
      if (kind == 0) {
        const std::size_t n = rng() % (cap + 2);
        ocg::IqBuffer in(n);
        for (auto& x : in) {
          x = {value, -value};
          value += 1.0F;
        }
        const bool fits = n <= cap && model.size() + n <= cap;
        require(r.push(in) == fits, "random: push accepts exactly when it fits");
        if (fits) {
          for (const auto& x : in) model.push_back(x.i);
          model_next += n;
        } else {
          value -= static_cast<float>(n);
        }
      } else if (kind == 1) {
        const std::uint64_t earliest = model_next - model.size();
        const std::uint64_t seq = earliest + rng() % (model.size() + 2);
        const std::size_t n = rng() % (cap + 2);
        ocg::IqBuffer out(n);
        const bool valid = n == 0 || (seq >= earliest && seq + n <= model_next);
        require(r.read(seq, out) == valid, "random: read succeeds exactly when in range");
        for (std::size_t i = 0; valid && i != n; ++i) {
          const float want = model[static_cast<std::size_t>(seq - earliest) + i];
          require(out[i].i == want && out[i].q == -want, "random: read returns the pushed samples in order");
        }
      } else {
        const std::uint64_t earliest = model_next - model.size();
        const std::uint64_t seq = earliest + rng() % (model.size() + 2);
        r.discard_before(seq);
        while (!model.empty() && model_next - model.size() < seq) model.pop_front();
      }
      require(r.size() == model.size() && r.next_sequence() == model_next, "random: size and sequence track the model");
    }
  }

  // view / reserve / commit (S15): in-place access matches read() and push(),
  // wrapping ranges fall back (empty span), and draining the ring keeps the
  // tail where a producer may hold a reserved span.
  {
    ocg::IqRing r(6);
    auto tail = r.reserve(4);
    require(tail.size() == 4, "reserve: contiguous tail from empty");
    for (std::size_t i = 0; i != 4; ++i) tail[i] = {static_cast<float>(i + 1), 0.0F};
    require(r.size() == 0, "reserve: nothing visible before commit");
    r.commit(4);
    require(r.size() == 4 && r.next_sequence() == 4, "commit publishes the samples");
    auto v = r.view(1, 3);
    require(v.size() == 3 && v[0].i == 2.0F && v[2].i == 4.0F, "view returns the committed samples in place");
    require(r.view(2, 3).empty(), "view refuses a range beyond the frontier");
    require(r.reserve(3).empty(), "reserve refuses more than the free space");
    r.discard_before(4); // drain: tail must stay at storage index 4
    auto t2 = r.reserve(2);
    require(t2.size() == 2 && t2.data() == tail.data() + 4, "drain keeps the tail position");
    t2[0] = {5.0F, 0.0F};
    t2[1] = {6.0F, 0.0F};
    r.commit(2);
    require(r.reserve(3).size() == 3, "reserve after wrap point starts at storage index 0");
    const ocg::IqBuffer three{{7.0F, 0.0F}, {8.0F, 0.0F}, {9.0F, 0.0F}};
    require(r.push(three), "push after the tail wraps");
    require(r.view(5, 2).empty(), "view refuses a range that wraps in storage");
    ocg::IqBuffer out(2);
    require(r.read(5, out) && out[0].i == 6.0F && out[1].i == 7.0F, "read still serves a wrapping range");
  }

  // Empty out-span read short-circuits to true regardless of ring state.
  {
    ocg::IqRing r(4);
    ocg::IqBuffer empty;
    require(r.read(0, empty), "empty read returns true even on an empty ring");
  }

  return 0;
}
