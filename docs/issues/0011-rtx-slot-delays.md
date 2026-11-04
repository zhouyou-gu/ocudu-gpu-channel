# #11: RTX: strict slot timing still fails after broker clock recovery

GitHub: [#11](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/11) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** Native OCUDU/srsUE transport and Linux CPU placement

**Platform:** Intel Core Ultra 9 285K + RTX 5090; CPU PHY, CUDA broker

**Category:** Measured timing failure

## Observed and expected behavior

A 30-second corrected-placement control reached 1000.320 slots/s but retained an 8.352 ms slot gap and 55 whole-process input starvations. A subsequent trace captured 7.315 and 6.828 ms gaps.

Meet the documented strict timing gate while preserving IQ continuity and avoiding overflow or blocked radio workers.

## Reproduction and scope

Use the native single-cell scheduler benchmark with two single-port srsUEs, mixed traffic, moving robot-ring seed 1, 20 MHz / 15 kHz / 23.04 MS/s and the reviewed clock-recovery broker. Keep diagnostics short and distinguish traced from untraced runs.

## Findings and unknowns

One captured gap followed a 6.215 ms ready-but-not-running wait in FIFO gNB ZMQ I/O on CPU 7 during bound display polling (output_poll_execute). The broker had already sent the IQ reply. Another followed a 5.934 ms UE SYNC CPU wait behind background physical-AI Python. IRQ placement and an unbound workqueue mask did not exclude the bound display callback.

These are two causal captures, not attribution of every miss. A continuously polling radio worker at FIFO priority also introduced periodic long pauses in a rejected control; normal scheduling at nice −10 removed that pattern.

## Closure checks

Verify effective CPU placement for the complete IQ dependency path and bound kernel work, then pass the documented strict timing and buffer/continuity checks on the declared supported configuration. Retain independent gNB and UE clocks.

## Evidence

[docs/reports/validation/live-runtime-limitations-20261104.md](../reports/validation/live-runtime-limitations-20261104.md), including the November clock-recovery and tracing findings.

[Issue index](README.md)
