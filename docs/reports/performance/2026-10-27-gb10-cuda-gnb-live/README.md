# CUDA-accelerated OCUDU gNB on the DGX Spark GB10: live scaling measurements (2026-10-26/27)

Author: minwoo. Measured on one DGX Spark (GB10) with the broker of `ehs0/ocudu-gpu-channel` branch `RAN_benchmark` at 364391b (source identical to upstream main 5f11fd2 except the opt-in sample-indexed control) and real OCUDU gNBs and srsUE / OAI nrUE radios over ZMQ. Data, runners, summarisers and local patches are listed in [Data and reproduction](#data-and-reproduction).

On one DGX Spark (GB10) the CUDA-accelerated OCUDU gNB (all PHY stages on the GPU) reproduces the CPU-PHY gNB's live scaling curves within ±0.03 on every axis measured on 2026-10-26/27, with one exception: a four-antenna gNB at 100 MHz, where the CPU PHY itself is the wall (0.16–0.17) and the CUDA gNB reaches 0.45–0.46. Everywhere else the live loop is bound by CPU work the offload does not touch (the gNB's MAC and upper layers, the UE's per-slot processing), and GPU sharing without MPS or a second GPU-heavy cell makes the CUDA gNB slower, not faster.

## Summary

- UE count, 20 MHz (srsUE): 1 / 2 / 4 UEs real time, 8 UEs 0.92, 16 UEs 0.45 with either gNB. At 16 UEs the gNB turnaround is 1.10 ms with the CUDA gNB against 1.05 ms with the CPU gNB, the UE turnaround 1.13 against 1.10 ms: PHY offload removes none of it.
- Bandwidth and antennas, one OAI nrUE: 1x1 real time at 20 / 50 / 100 MHz with both gNBs; 2x2 identical to the CPU gNB idle and under 200 Mb/s; 4x1 / 4x2 / 2x4 / 4x4 identical at 20 MHz and within 0.1 at 50 MHz. At 100 MHz with four gNB antennas the CUDA gNB is 2.7x the CPU gNB (0.45 against 0.17); with four UE antennas both land on 0.33–0.44.
- Bandwidth and UE count, two and four OAI nrUEs: identical curves (20 MHz 0.99 / 0.95–0.98, 50 MHz 0.99 / 0.8, 100 MHz 0.85 / 0.50).
- GPU sharing: only free under MPS. Broker and CUDA gNB in separate CUDA contexts: 1 UE 0.93–0.96, 8 UEs 0.71, 16 UEs 0.34. Two cells with two CUDA gNBs: 532 / 526 slots per second against 877 / 884 with CPU gNBs (four GPU clients lift the broker's channel call from 70 to 190 µs).
- The channel emulator was never the limit in any of these runs: its channel call per node stayed at 36–142 µs per slot throughout; what grew was its wait for IQ from the radios.

## Setup

CUDA gNB: OCUDU `nvcuda_accel_02` at commit 5830c9cb with the project's C1 + D8 + D9 + D10 patches, built for sm_121 (`builds/d10-cuda-patched-sm121`, lock `integrations/ocudu/cuda-workspace.spark-d10.lock.json`), all stages on (lower-PHY RX/TX, PUSCH, PDSCH, PRACH with its lower-PHY demodulator, SRS; managed grid). About 9.5 GB unified memory, 60–150 s start. Reference: CPU-PHY build `builds/ocudu-zmq-release` (OCUDU a1916ed).

| Item | CPU gNB | CUDA gNB |
| --- | --- | --- |
| GPU sharing | broker alone on the GPU | broker and gNB in one MPS server; separate contexts as a control |
| gNB run-ahead | available (lead lower-PHY build) | not available: that patch and CUDA do not combine |
| CPU placement | gNB 5–12, broker 15–16, UEs 17–19 (+13, 14 from 8 UEs); OAI gates gNB 5–9, broker 15–17, UE 18–19 | same |
| Channel | ehs0 broker 364391b unchanged; Sionna SUTD scene (UE sweeps), fixed MIMO matrices (antenna runs) | same |

Measurements: RAN_benchmark single-cell scheduler benchmark (30 s windows after 10 s warm-up, two runs per session, four audit gates) and two-cell benchmark; lead-tree OAI 1x1 and 2x2 gates (2x2 gate given antenna knobs: gNB 2 or 4 ports, UE 1/2/4 ports, DFT-based fixed MIMO, DL rank ≤ 2 by the OAI UE); the branch's OAI multi-UE gate (2 or 4 nrUEs, TDD n78 30 kHz, idle). Real-time ratio = radio samples per wall second / nominal rate on the gNB link. Local runner knobs (none in the broker): SINGLE_CELL_GNB_BINARY / _GNB_ACCELERATION / _MPS / _BROKER_IN_MPS / _GNB_START_TIMEOUT, two-cell equivalents, OCUDU_NATIVE_GNB_ACCELERATION, OAI2X2_GNB_ANT / OAI2X2_UE_ANT.

## Results

UE count, one 20 MHz cell (srsUE, mixed traffic). CPU series = stock gNB without run-ahead. 1–8 UEs pass all four audit gates with either gNB.

| UEs | 1 | 2 | 4 | 8 | 16 |
| --- | ---: | ---: | ---: | ---: | ---: |
| CPU gNB | 0.996 | 0.997 | 0.998 | 0.93 | 0.45 |
| CUDA gNB, MPS | 0.998 | 0.997 | 0.995 | 0.92 | 0.45 |
| CUDA gNB, no MPS | 0.93–0.96 | — | — | 0.71 | 0.34 |

Two cells: CPU gNBs 877 / 884 slots/s per cell (0.88); CUDA gNBs under one MPS server 532 / 526 (0.53); without MPS 376 / 394.

Bandwidth × antennas, one OAI nrUE, idle cell, CPU / CUDA gNB (one run per point):

| gNB × UE antennas | 20 MHz | 50 MHz | 100 MHz |
| ---: | ---: | ---: | ---: |
| 1x1 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| 2x2 | 1.00 / 1.00 | 0.98 / 0.99 | 0.66 / 0.69 |
| 4x1 | 1.00 / 1.00 | 0.71 / 0.82 | 0.17 / 0.45 |
| 4x2 | 1.00 / 1.00 | 0.65 / 0.72 | 0.16 / 0.46 |
| 2x4 | 1.00 / 1.00 | 0.60 / 0.63 | 0.42* / 0.44* |
| 4x4 | 1.00 / 1.00 | 0.49 / 0.51 | 0.33* / 0.34* |

\* attach/PDU/ping pass; gate health fails (PUSCH error ratio 0.12–0.15 > 0.10, four UE TX antennas at 100 MHz). 2x2 under 200 Mb/s DL: 0.88 / 0.84, 0.37 / 0.38, 0.28 / 0.29.

Bandwidth × UE count, 2 and 4 OAI nrUEs (TDD n78 30 kHz, idle, CPU / CUDA): 20 MHz 0.994 / 0.994 and 0.95 / 0.975; 50 MHz 0.986 / 0.988 and 0.79 / 0.82; 100 MHz 0.84 / 0.85 and 0.50 / 0.50.

## Where the time goes

The radios deliver their IQ late because their per-slot computation exceeds the slot on the cores they have; two structural effects exist and were measured, and only one of them matters.

- Computation, measured: srsUE full-band decode 1.6–2.9 ms/slot before the NEON LDPC fix (4 UEs 0.57 → 0.99 after). OAI UE at 100 MHz keeps 1.55 of 2 cores busy, no idle. Four gNB antennas at 100 MHz: offloading the gNB PHY lifts 0.17 → 0.45 at once. Four UE antennas: the ratio falls with antenna count regardless of gNB.
- The broker is not in this path: channel call per node 36–142 µs (kernel 24–117 µs) in every antenna run; what grows is its wait for input IQ (700–1,600 µs per slot at 50 MHz, 2.7 ms at 100 MHz 4x1 CPU).
- Structure, lock step: DL block j waits for UL block j (serial gNB → broker → UE → broker → gNB per slot). Cost measured with a 2-slot gNB run-ahead: 4 UEs 0.993 → 0.998, nothing at 8/16 UEs, nothing under load at 50/100 MHz. Marginal only.
- Structure inside the OAI UE: downlink processing runs in one actor thread per slot (36 % of samples), so extra cores widen only the LDPC pool: 5 big cores instead of 2 take 100 MHz 2x2 loaded from 0.24 to 0.35, not to real time. Computation and structure are two faces of the same limit.
- 16 UEs at 20 MHz: 1.05–1.13 ms turnarounds on both sides with either gNB; on the gNB what remains is MAC, scheduler, upper layers, ZMQ radio. Compute vs single-thread not split (gNB not profiled).
- GPU sharing without MPS time-slices broker vs gNB each slot (1 UE 0.93–0.96, 8 UEs 0.71, 16 UEs 0.34); two CUDA gNBs add two clients, broker call 70 → 190 µs.

## When PHY offload pays

- Where the gNB PHY is the slowest stage of the slot: four gNB antennas at 100 MHz on this box; not at 20 MHz, not at 2x2, not at any UE count with srsUE.
- It needs MPS with the broker in the same server and does not combine with the run-ahead patch today.
- It does not pay for a second cell on the same GB10.
- After it, the next wall is the OAI UE (one DL actor thread, four RX antennas) and, at 16 UEs, the gNB's non-PHY CPU work.

## Problems met and fixes

| Problem | Cause | Fix |
| --- | --- | --- |
| `Accelerated lower-PHY PRACH demodulation failed` etc., 380k-line VkFFT dumps | `nvrtc: error: failed to open libnvrtc-builtins.so.13.0` (non-login shell without LD_LIBRARY_PATH=/usr/local/cuda/lib64) | export it in runners and gate wrappers |
| `cuda-lphy-sm121` (run-ahead + CUDA) attaches but no PDU session | CUDA lower-PHY TX stalls (126 slots/50 s) | use D10; run-ahead and CUDA do not combine |
| "MPS on" measured nothing | runner kept the broker outside MPS | SINGLE_CELL_BROKER_IN_MPS=1 (broker outside MPS: 1 UE 0.998 → 0.93–0.96) |
| first test of first CUDA sessions missed the 5 s replay-bridge reply | cold kernel JIT | start timeout 150 s |
| `cudaSetDevice failed: out of memory` with 38 GB free | page cache on unified memory | 48 GB anonymous allocation in the container |
| one OAI UE died at 50 MHz (CPU gNB, 2-UE gate) | OAI assertion on a bad DCI | repeat passed; unrelated to gNB build |
| 2x4 / 4x4 at 100 MHz fail the health bound with either gNB | PUSCH error ratio 0.12–0.15 with four UE TX antennas | reported as is; UE at its limit |

## Not measured

CUDA gNB + run-ahead (no build has both); partial acceleration stages; 32 UEs; 4 broker I/O threads with CUDA gNB; other scenes/seeds/schedulers/traffic; multi-antenna cells with >1 UE or under load (except 2x2); srsUE above 20 MHz (LTE PRACH library, 2048-point FFT cap); gNB perf at 16 UEs; each point one session (two 30 s runs) or one gate run.

## Data and reproduction

| File | Contents |
| --- | --- |
| `data/srsue-ue-sweep-20mhz.csv` | single-cell sessions g105–g125: UE count, gNB, MPS, per-run real-time ratio, audit gates |
| `data/two-cell.csv` | two-cell slots per second, CPU and CUDA gNBs, MPS on/off |
| `data/oai-1ue-bandwidth-antennas.csv` | OAI 1-UE gate runs: 1x1, 2x2 (idle and 200 Mb/s) and the 4x1 / 4x2 / 2x4 / 4x4 sweep at 20 / 50 / 100 MHz, gate real-time factor, health, broker-heartbeat ratio |
| `data/oai-multi-ue-bandwidth.csv` | OAI 2- and 4-UE gate runs at 20 / 50 / 100 MHz |
| `data/hop-trace-16ue.csv` | 16-UE turnaround p50 per side, CPU and CUDA gNB |
| `tools/*.patch` | the local runner / gate knobs (none touch the broker) |
| `tools/*campaign*.sh`, `tools/*summ*.py`, `tools/gen-nxm-topology.py`, `tools/hopturn.py` | campaign scripts, summarisers, N×M topology generator, hop-trace turnaround tool |

Single-cell sessions `/workspace/ocudu-spark-ranb/results/single-cell-scheduler-benchmark/tagged-*-g105…g125`; two-cell `…/reports/ocudu-scheduler-benchmark/20261026T131042Z` (MPS), `20261026T131405Z` (no MPS); OAI 1x1/2x2 `/workspace/gpuch/lead/runs/c1x1-*`, `c2x2-*`; antenna sweep `/workspace/gpuch/lead/runs/n{4x1,4x2,2x4,4x4}-{20,50,100}i-{cpu,cuda}-*` (campaign `/workspace/gpuch/bound/nxm/`); OAI 2/4-UE `/workspace/ocudu-spark/results/reports/ocudu-oai-multi-ue/20261027T06…–07…Z` (`/workspace/gpuch/bound/oaimue/`); hop traces `/workspace/gpuch/hop-hop-{cd10,cpu}-16ue/`; patches `~/ocudu-work/share/ranb-cuda-gnb-and-bw-knobs-2026-10-27.patch`, `ranb-oai-multi-ue-bw-knobs-2026-10-27.patch`, `oai-gate-nxm-antennas-2026-10-27.patch`; long form `~/ocudu-work/spark-scaling-proposal-2026-10-22.md` §3–5, 8. Measured code: broker ehs0/ocudu-gpu-channel RAN_benchmark 364391b (unchanged), srsUE with the NEON LDPC patch, OAI nrUE 2026.w33 with the patched ZMQ module.
