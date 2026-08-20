# Live gate results

Measured results for every gate in `scripts/remote/`, recorded so a reader can
see what has actually been demonstrated rather than inferring it from the fact
that a script exists.

**Host**: Intel Core Ultra 9 285K, one RTX 5090 (32 GB), driver 580.173.02,
CUDA 12.8.93, Ubuntu 24.04.
**Date**: 2026-08-19.
**Branch**: `minwooeun-rank1-miso-simo-review-fixes`.
**Stack**: OCUDU gNB + Open5GS 5GC + srsRAN_4G srsUE `release_23_11`, all
containerised, driven through the CUDA broker. Every gate was run from a clean
`git clone` of the branch with only `.config` added.

Every srsUE keeps `nof_antennas = 1`. All claims are rank-1 MISO/SIMO.

## Single-UE gates

| Gate | Result | Live y=Hx |
|---|---|---|
| `ocudu-attach-smoke.sh` (1×1) | **pass** | n/a |
| `ocudu-rank1-2x1-smoke.sh` (2T2R) | **pass** | UL rows 4.59e-05 / 2.06e-05 · DL row 4.86e-08 |
| `ocudu-rank1-4x1-smoke.sh` (4T4R) | **pass** | UL rows 4.54 / 2.16 / 1.68 / 2.76 e-05 · DL row 4.86e-08 |

All with RRC connected, PDU session established, user-plane ping landing, and
`tx_queue_overflows = tx_sequence_gaps = zmq_errors = 0`.

The uplink rows reproduce the figures originally reported from the native
harness (4.6 / 2.2 / 1.7 / 2.8 e-05) row by row, on different hardware, a
different 5GC deployment and a different container stack.

## Multi-UE gates

Two or more srsUEs on one multi-antenna cell. Every gNB receive row is then the
sum of all the UEs' uplink columns, and the matrix checker reconstructs each row
from all incoming links, then removes them one at a time and requires the match
to break.

| Gate | UEs | Result |
|---|---|---|
| `ocudu-multi-ue-smoke.sh` (1T1R) | 2 | **pass** |
| `ocudu-rank1-2x1-multi-ue-smoke.sh` (2T2R) | 2 | **pass** — rows 4.54e-05 / 4.53e-05; removing either UE breaks the match by 91.3 to 236.9 |
| `ocudu-rank1-4x1-multi-ue-smoke.sh` (4T4R) | 2 | **pass** — rows 6.76 / 5.33 / 2.91 / 3.99 e-05 |
| `ocudu-rank1-2x1-triple-ue-smoke.sh` (2T2R) | 3 | **blocked** — 1 of 3 truly attaches; two share a C-RNTI |
| `ocudu-rank1-2x1-quad-ue-smoke.sh` (2T2R) | 4 | **blocked** — 1 of 4 truly attaches; three share a C-RNTI |

Control experiments used for the diagnosis below, not gates:
`examples/topology.ocudu-docker.multi-ue-quad.cuda.yaml` (stock single-antenna
cell, four UEs) and `examples/ocudu/gnb_zmq_b210_fdd_1t1r_mimo_settings_bisect.yaml`
(single antenna carrying the MIMO cell settings).

The three- and four-UE gates are committed as reproducible investigations, not
as passing gates. Do not cite them as demonstrated capability.

## Why three and four UEs do not attach

**Root cause: every UE sat at the same propagation delay, so the gNB could not
tell their preambles apart.**

srsUE always transmits preamble index 0. When several UEs share a PRACH
occasion, the only thing that separates them at the receiver is timing advance —
exactly as in a real cell, where UEs at different distances produce correlation
peaks at different offsets. Every link in the multi-UE fixtures declared
`delay_samples: 0.0`, so four identical preambles arrived at the identical
instant and summed into ONE correlation peak.

The gNB's own detector output says it plainly:

```
detected_preambles=[{idx=0 ta=0.00us detection_metric=86.9 power_dB=15.13}]
```

One peak, `ta=0.00us`, for the whole run. The gNB was not ignoring the other
UEs; it could not see them. Only one RA procedure ever existed, so only one
TC-RNTI was ever allocated, and the remaining UEs decoded that RAR — its
RA-RNTI is derived from the PRACH occasion position and its RAPID matched their
preamble 0 — and adopted its TC-RNTI. Three processes then reported
`RRC Connected` on one identity while only one held a PDU session.

Giving each UE a distinct delay (0 / 16 / 32 / 48 samples) changes the gNB's
view completely:

| | identical delays | distinct delays |
|---|---|---|
| PRACH detections | 1 (whole run) | **204** |
| TC-RNTIs allocated | 1 (`0x4601`) | **204** (`0x4601`…`0x46cc`) |

So the detection collapse is fixed and the diagnosis is settled. Attach still
does not complete — the detections are weak (`detection_metric` 86.9 → 3.6) and
still report `ta=0.00us` where a 16-sample delay should read ≈0.69 µs — so
something downstream of preamble detection remains. That is the open item.

The emulator's delay itself is **not** the problem, and this was checked rather
than assumed. A synthetic capture with a declared 16-sample delay reproduces it
exactly:

```
lag=0    max|y - h*x[n-lag]| = 1.86e-02
lag=15   max|y - h*x[n-lag]| = 1.16e-03
lag=16   max|y - h*x[n-lag]| = 1.31e-07   <- declared value
lag=17   max|y - h*x[n-lag]| = 1.16e-03
```

### The three defects, separated

Running the delay fix together with a cold-source broker change (a node advances
while a peer has never connected, treating it as silence) isolates the remaining
work cleanly:

```
ue0  c-rnti=0x4601  RA=1    rrc=1  pdu=1  ip=10.45.1.2   <- clean attach
ue1  c-rnti=0x4601  RA=1    rrc=1  pdu=0                 <- decoded ue0's RAR
ue2  c-rnti=0x4601  RA=1    rrc=1  pdu=0                 <- same
ue3  RA=200, no c-rnti                                   <- never detected
PRACH: 1   detected_preambles=[{idx=0 ta=0.00us detection_metric=86.9}]
```

Detection is strong again (86.9, not the 3.6 seen without the broker change) and
the first UE attaches properly. So three independent defects, not one:

| # | Defect | Status |
|---|---|---|
| 1 | Identical propagation delay collapses all preambles into one peak | **fixed**, this commit |
| 2 | A cell cannot run until every UE's link is live | fix works but trips `tx_queue_overflows` |
| 3 | A late-joining lane replays its peer's backlog and jams | **unsolved** |

Defect 3 is why defect 2's benefit stops at the first UE. A lane that goes live
after the node has started keeps its cursor at 0, so the node replays that
peer's stream from its first sample. Under the real-time throttle the backlog
never shrinks: the ring pins at capacity (measured 2396160/2457600 with over a
million puller room stalls), the puller stalls, and that UE's srsUE blocks in
`tx()` before its preamble reaches a slot the gNB reads.

Three cursor policies were tried for it and none is right:

| Policy | Result |
|---|---|
| Replay from sequence 0 | ring jams at 97%, UE freezes |
| Snap to the live frontier | discards the startup head start; relay stalls at `tx_pulls=6` |
| Snap only once the node has produced | `tx_queue_overflows` 317, still one PRACH |

The head start matters because it is the lock-step radios' only timing slack --
the epoch co-init comment in `broker.cpp` warns of exactly this. The likely
correct answer is re-establishing a *common* epoch across all lanes when a
participant joins, rather than patching one lane's cursor, which is a design
decision about what a running cell does when a new radio appears and is left
open deliberately.

### Corrections to earlier explanations

Three explanations were published in this branch before this one and are all
wrong. They are recorded because each was disproved by a specific measurement:

- *"The gNB merges the preambles onto one C-RNTI."* It issues a fresh C-RNTI per
  detected preamble; `rnti_manager::allocate()` increments until it finds a free
  one, so it cannot reuse a registered RNTI. Only one preamble was ever
  detected.
- *"`ss2_type: ue_dedicated`, which multi-antenna downlink forces, is the
  blocker."* Reverting it appeared to fix four UEs, but the C-RNTIs show three
  of those four were the same UE and the PDU count stayed at 1. `ss2_type`
  changes only how the losers fail, not how many attach.
- *"The multi-source stall freezes virtual time, so UEs cannot be separated."*
  The stall is real, but UEs sharing an occasion is survivable when they are
  physically distinguishable; separation in time was never the binding
  requirement.

`RRC Connected` is not proof of attach: srsUE reports it even when it lost
contention resolution. Judge multi-UE results on distinct C-RNTIs and completed
PDU sessions.

## Synthetic control

Before any of the live multi-UE work, the superposition arithmetic was checked
in isolation: a 2-port gNB against two single-port synthetic peers, no RAN stack
involved.

```
gnb0 rx row0: max|y-(h_ue0*x0 + h_ue1*x1)| = 2.044e-07   [only ue0: 4.472e-01 | only ue1: 7.566e-01]
gnb0 rx row1: max|y-(h_ue0*x0 + h_ue1*x1)| = 1.077e-07   [only ue0: 6.265e-01 | only ue1: 2.915e-01]
```

The single-user hypotheses are wrong by 0.29 to 0.76, so the engine sums both
links per row rather than dropping one. The engine's multi-user support is
therefore not in question -- the blockage above is in the attach procedure, not
the channel.

## Other verification on this host

| Check | Result |
|---|---|
| `gpu-test-sequence.sh` | 9/9 pass |
| `ctest`, CUDA and CPU trees | 8/8 each |
| `ctest` under ASan + UBSan + LeakSanitizer | 8/8 clean |
| ThreadSanitizer, `ctest` plus a live relay | no race in project code; all reports inside uninstrumented libzmq |
| Synthetic y=Hx: 2×2, 2×1/1×2, 4×1/1×4, both backends | pass, all rows <= 1.44e-07 |
| CPU vs CUDA parity | identical row RMS on the same topology |

## Whole-run latency

Node process latency, every slot recorded (`event=process_latency_summary`),
from the live gates:

| Config | Node | n (slots) | p50 | p95 | p99 | p99.9 |
|---|---|---|---|---|---|---|
| 2×1 | gnb0 | 57,753 | 80 µs | 135 µs | 205 µs | 340 µs |
| 2×1 | ue0 | 54,794 | 75 µs | 150 µs | 230 µs | 380 µs |
| 4×1 | gnb0 | 54,439 | 115 µs | 200 µs | 285 µs | 675 µs |
| 4×1 | ue0 | 54,284 | 120 µs | 210 µs | 310 µs | 650 µs |

GPU kernel p50: 10.6 µs (2×1), 12.9 µs (4×1). Everything through p99.9 fits the
1 ms slot budget. The observed maximum in both configurations lands in the 5 ms
overflow bucket; those slots are under 0.1% of the run and appear to sit at run
start and teardown, but that has not been attributed and is an open item.
