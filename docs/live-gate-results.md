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
| `ocudu-rank1-2x1-triple-ue-smoke.sh` (2T2R) | 3 | **blocked** — all three reach RRC, PDU session unreliable |
| `ocudu-rank1-2x1-quad-ue-smoke.sh` (2T2R) | 4 | **blocked** — only the last-started UE attaches |

Control experiments used for the diagnosis below, not gates:
`examples/topology.ocudu-docker.multi-ue-quad.cuda.yaml` (stock single-antenna
cell, four UEs) and `examples/ocudu/gnb_zmq_b210_fdd_1t1r_mimo_settings_bisect.yaml`
(single antenna carrying the MIMO cell settings).

The three- and four-UE gates are committed as reproducible investigations, not
as passing gates. Do not cite them as demonstrated capability.

## Why three and four UEs do not attach

**The cause is `ss2_type: ue_dedicated`, which multi-antenna downlink forces.**
It is not the MIMO channel, not the emulator's superposition, and not the
multi-source stall. It was isolated by bisection, and two earlier explanations
were wrong and are corrected below.

### The bisection

Each row is four srsUEs on one cell, everything else held constant.

| Antennas | Cell settings | UEs reaching RRC |
|---|---|---|
| 1T1R | stock (`ss2_type: common`) | **4 / 4**, one RA procedure each |
| 2T2R | rank-1 MIMO set (`ss2_type: ue_dedicated`, CSI-RS off, `max_ue_mcs: 9`, pcap off) | **1 / 4** |
| 1T1R | the MIMO set applied to a *single-antenna* cell | **1 / 4** |
| 1T1R | the MIMO set, but `ss2_type` reverted to `common` | **4 / 4**, one RA procedure each |

Rows two and three isolate it away from the antenna count: one antenna with the
MIMO settings fails exactly as two antennas do. Rows three and four isolate the
single setting: reverting only the search-space type, while keeping CSI-RS off,
the MCS cap and pcap disabled, restores all four attaches.

### Why this is a hard conflict

The rank-1 fixtures do not choose `ue_dedicated` freely. OCUDU's validator
forbids fallback DCI in SS#2 when `nof_antennas_dl > 1`
(`du_cell_config_validation.cpp:290`), so any multi-antenna downlink cell must
use `ss2_type: ue_dedicated` with DCI 0_1/1_1. That is the same adaptation the
single-UE rank-1 gates needed and documented.

So **multi-antenna downlink and multi-UE scale are in direct conflict on this
stack**, through one setting neither side can give up: multi-antenna requires
`ue_dedicated`, and beyond about two UEs `ue_dedicated` prevents the rest from
completing random access. Two UEs works because it stays under that threshold.

### A separate, pre-existing limitation

Even with `ss2_type: common` and four UEs attaching, only one reaches a PDU
session. The stock single-antenna four-UE control behaves identically -- 4/4 RRC,
1/4 PDU. That failure predates this work, is unrelated to MIMO, and is not
diagnosed here.

### Corrections to earlier explanations

Two explanations were published and are wrong:

- *"The gNB merges the preambles onto one C-RNTI."* It does not. Preamble
  detection and RAR both work and a fresh C-RNTI is issued per attempt.
- *"The multi-source stall freezes virtual time, collapsing the start stagger,
  so N-1 UEs collide at Msg3."* The stall is real and observable
  (`event=node_stall node=gnb0 phase=input_data`), and UEs do start on the same
  PRACH occasion because of it. But it is not what blocks attach: with
  `ss2_type: common` the UEs still share that occasion, and all four attach
  anyway. Shared occasions are survivable; `ue_dedicated` is not.

A near/far power spread across the four UEs was also tested, on the theory that
equal receive power removed the capture effect during contention. It changed
nothing, and the spread is retained in the fixture only because it is more
realistic.

### Attempted fix, and why it was reverted

The second mechanism has an obvious fix: let a destination advance while a
source has never connected, treating that source as contributing silence. An
absent radio does transmit nothing, and it would also let a UE join a running
cell. It was implemented and reverted; recorded here so the next attempt starts
from the result rather than repeating it.

The change was narrow. A lane whose source ring had never produced a sample was
excluded from the `common = min(available)` window calculation, fed zeros for
that slot, and had its cursor held still so it would enter at its peer's true
start once it connected. Cursor co-initialisation was deferred until every lane
had joined, so a late peer still got a correct epoch. It builds, and `ctest`
stays 8/8.

On the live four-UE gate it is **worse than the problem it targets**:

```
before: gnb0 stalls on input_data;      1 of 4 UEs attaches
after:  gnb0 stalls on output_room x72, 0 of 4 UEs even transmit,
        gnb0 TX ring empty
```

Uplink and downlink stop advancing together. Once the gNB node can process
uplink slots against silence it runs ahead and fills its own receive rings,
while the gNB container -- a lock-step ZMQ radio that alternates transmit and
receive -- is still waiting to be pulled. Its transmit ring drains to empty, the
UEs lose downlink and never sync, and the relay deadlocks with the receive side
full and the transmit side starved.

So the input-window rule is not merely conservative. It is what keeps a
lock-step radio's two directions in step, and relaxing it for cold sources needs
a paired mechanism -- bounding how far a node may run ahead of its slowest
*live* peer, or gating on the destination radio's own consumption rather than
only on output room. The one-sided version does not survive contact with a real
radio.

## What would have to change

Two independent things, either of which alone leaves four UEs blocked:

1. **The search-space conflict**, which is the binding one. Multi-antenna
   downlink forces `ss2_type: ue_dedicated`, and beyond about two UEs that
   prevents the rest completing random access. This lives in the RAN stack, not
   the emulator, so it is a question for OCUDU rather than something this
   repository can configure around.
2. **Cold-source admission**, per the reverted attempt above, so UEs can join a
   running cell and start against an advancing clock instead of all landing on
   one PRACH occasion.

Until both are addressed, **two UEs per cell is the supported multi-user
configuration**, and it is verified.

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
