# #18: Spark: CPU-PHY UE scaling falls below real-time rate at 8 and 16 UEs

GitHub: [#18](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/18) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** CPU OCUDU/srsUE capacity and CPU allocation

**Platform:** DGX Spark GB10; CPU PHY with CUDA channel broker

**Category:** Measured capacity limitation

## Observed and expected behavior

The dated one-gNB Spark campaign measured approximately 714–727 slots/s at 8 UEs and 300–323 at 16 UEs with the original allocation. More UE CPU access improved these to 855–858 and 455–469, respectively, still below 1000 slots/s.

A claimed real-time UE capacity requires the gNB and every UE to meet the documented timing gate, not merely attach or pass an input-comparison audit.

## Reproduction and scope

Use the archived runner 364391b campaign inputs: 20 MHz / 15 kHz / 23.04 MS/s, mixed traffic, seeded moving SUTD channel replay and the recorded NEON srsUE build. Compare exact input hashes and audit every radio clock.

## Findings and unknowns

Changing only UE CPU eligibility improved overloaded runs, establishing CPU availability as a contributor. Synthetic channel-only latency and separate GPU-PHY contributor runs have different scopes.

No tested UE count obtained strict real-time qualification. CPU decoding, transport and scheduling costs have not been fully separated; GPU PHY offload alone is not a demonstrated remedy.

## Closure checks

Define and validate the supported capacity envelope with independent gNB/every-UE clocks, runtime counters and packet delivery. Any allocation or implementation improvement must use matched inputs and preserve the declared workload.

## Evidence

[docs/reports/validation/live-runtime-limitations-20261104.md](../reports/validation/live-runtime-limitations-20261104.md), [docs/getting_started/status.md](../getting_started/status.md). The limitation report includes the compact Spark campaign summary and November timing review.

[Issue index](README.md)
