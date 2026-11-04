# #17: Qualify live downlink activity on every claimed gNB antenna branch

GitHub: [#17](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/17) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** Native gNB transmit-port activity and live multi-port validation

**Platform:** Rank-1 multi-port gNB with single-port srsUE

**Category:** Live evidence coverage gap

## Observed and expected behavior

The tested srsUE rank-1 live downlink radiates only gNB port 0. Synthetic tests and independent IQ oracles demonstrate multi-branch computation, but that does not prove a live downlink with every claimed transmit branch active.

A live multi-branch downlink claim includes nonzero activity on the relevant source ports and independent verification of the resulting channel sum.

## Reproduction and scope

Use the recorded 2×1 or 4×1 native rank-1 gate, capture bounded source IQ per gNB transmit port, verify each active branch, and independently recompute the declared matrix output.

## Findings and unknowns

Multi-port transport, attachment and CUDA matrix arithmetic are implemented and tested. The observed limitation is the scope of live transmit activity and resulting evidence, not proof of a broken channel matrix implementation.

The supported gNB configuration or separately pinned adapter change that activates the required branches, and an appropriate live receiver validation, remain to be qualified. Multiple single-port UEs do not establish rank-2 decoding.

## Closure checks

Produce a reproducible native live case with every claimed downlink branch active and independent channel/receiver checks; retain rank-1/rank-2 and synthetic/live claim boundaries.

## Evidence

[docs/reports/validation/rank1-feasibility.md](../reports/validation/rank1-feasibility.md), [docs/reports/validation/rank1-boundaries.md](../reports/validation/rank1-boundaries.md), [docs/getting_started/status.md](../getting_started/status.md).

[Issue index](README.md)
