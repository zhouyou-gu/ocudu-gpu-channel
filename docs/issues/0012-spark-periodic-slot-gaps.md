# #12: Spark: periodic 8.9–9.7 ms slot gaps remain with separated application CPUs

GitHub: [#12](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/12) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** Native radio timing on GB10; exact blocking component unresolved

**Platform:** DGX Spark GB10, aarch64, Linux 7.0.0-1019-nvidia, CUDA 13.0

**Category:** Measured timing failure; cause unresolved

## Observed and expected behavior

The reduced-audit 30-second run reached gNB 999.979 slots/s and UE0/UE1 interior rates 1000.115/1000.135. Four consecutive-slot gaps were 9.749, 9.373, 9.203 and 8.872 ms at offsets 4.496, 9.496, 14.496 and 24.497 seconds. There were 14 whole-process starvations and 160962/160963 packets received; overflow, IQ gaps and ZMQ errors were zero.

No unexplained periodic deadline failures under the documented CPU-separated workload; near-1000 average progress alone is insufficient.

## Reproduction and scope

Repeat the recorded one-gNB/two-srsUE, CPU-PHY/CUDA-broker configuration with live Sionna 10 Hz, robot-ring seed 1, mixed traffic and 20 MHz / 15 kHz / 23.04 MS/s. Background applications used CPUs 0–4 and radio work 5–19. Recording this issue does not restart the stopped experiments.

## Findings and unknowns

Three placement snapshots and 0.5-second enforcement excluded background applications from radio CPUs. The retained audit showed no competing application CPU time. Kernel tasks still ran on those CPUs.

The roughly five-second pattern is observed, but no blocking thread/wait has been established. The audit lost 120 CPU-5 events. Do not attribute this Spark issue to the separately captured RTX display/Python causes.

## Closure checks

Capture one periodic pause with adequate coverage of PHY, UE, broker and driver dependencies; identify the first late handoff and validate a correction with independent radio clocks, strict counters and packet delivery. Keep traces bounded and preserve competing-service settings.

## Evidence

[docs/reports/validation/live-runtime-limitations-20261104.md](../reports/validation/live-runtime-limitations-20261104.md), including the November Spark timing findings.

[Issue index](README.md)
