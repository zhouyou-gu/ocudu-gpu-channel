#include "ocudu_gpu_channel/ring.h"
#include <cstdlib>
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

  // Empty out-span read short-circuits to true regardless of ring state.
  {
    ocg::IqRing r(4);
    ocg::IqBuffer empty;
    require(r.read(0, empty), "empty read returns true even on an empty ring");
  }

  return 0;
}
