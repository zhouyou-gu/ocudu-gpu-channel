# #15: Validate fractional-delay continuity at live hardware block boundaries

GitHub: [#15](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/15) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** Centered eight-tap fractional-delay filtering and hardware timing

**Platform:** Attached fractional-TDL / USRP hardware paths

**Category:** Known processing boundary and unqualified hardware behavior

## Observed and expected behavior

The centered eight-tap filter may require up to three future samples at a block boundary. Current hardware claims still need a validated guard or streaming treatment; absent future input can become zero-filled boundary support.

The claimed delayed channel agrees with continuous-stream filtering at block boundaries without hidden substitutions or missing required samples.

## Reproduction and scope

Use a bounded continuous known IQ sequence and fractional taps that require future support; compare separately processed blocks against an independent continuous-stream result, including every boundary. Then validate the declared attached-hardware timing.

## Findings and unknowns

Synthetic agreement and successful attachment do not establish correct fractional-delay support at live boundaries. Timing advance, finite filter tails and sample-aligned updates are separate parts of hardware qualification.

The supported guard/streaming mechanism and hardware timing envelope remain unqualified. This issue does not assert that every existing scalar or ZMQ-only channel result is incorrect.

## Closure checks

Implement and independently validate the required guard or streaming filter with explicit finite-tail/timing behavior; demonstrate boundary continuity and the relevant hardware timing checks before broadening the claim.

## Evidence

[docs/getting_started/status.md](../getting_started/status.md), [docs/concepts/timing.md](../concepts/timing.md).

[Issue index](README.md)
