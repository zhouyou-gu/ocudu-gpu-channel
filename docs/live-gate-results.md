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

The three- and four-UE gates are committed as reproducible investigations, not
as passing gates. Do not cite them as demonstrated capability.

## Why three and four UEs do not work yet

The failure is a Msg3 contention collision, caused by the emulator's virtual
clock collapsing the UE start stagger to zero.

**Virtual time is frozen while the cell is stalled.** A destination cannot
advance a slot until every incoming link has data, so a gNB with N UEs produces
nothing on the uplink until all N radios are running -- the broker logs
`event=node_stall node=gnb0 phase=input_data` with the producer in
`state=wait_data slots=0`. The UEs are deliberately started one at a time, but
that stagger is in wall-clock seconds while the UEs count frames in the broker's
virtual time, which is not advancing. Every UE started during the stall
therefore begins RACH at the *same* virtual instant.

Measured, four UEs with a 12 s wall-clock stagger between each:

```
ue0: ra-rnti=0x39, tti=334   ra-rnti=0x39, tti=494   ra-rnti=0x39, tti=654
ue1: ra-rnti=0x39, tti=334   ra-rnti=0x39, tti=494   ra-rnti=0x39, tti=654
ue2: ra-rnti=0x39, tti=334   ra-rnti=0x39, tti=494   ra-rnti=0x39, tti=654
ue3: ra-rnti=0x39, tti=974
```

Three UEs on the identical PRACH occasion, retrying in lockstep on the same
160 ms cadence. Only ue3 -- whose arrival is what ends the stall -- gets a
distinct start.

**Same occasion means same grant means collided Msg3.** Sharing a PRACH occasion
and preamble gives the same RA-RNTI, so those UEs decode the *same* RAR and
transmit Msg3 on the *same* PUSCH resources. At comparable receive power none of
them decodes. The gNB side shows exactly that -- preamble detection and RAR both
work, a fresh C-RNTI is issued per attempt, and the attempt dies at Msg3:

```
rnti=0x46bc h_id=0: Discarding UL HARQ process TB with tbs=11.
              Cause: Maximum number of reTxs 4 exceeded
rnti=0x46bd ... (a new C-RNTI every 16 slots, each failing the same way)
```

**This predicts the observed N.** N UEs put N-1 of them on the same occasion:

| N | Simultaneous colliders | Outcome |
|---|---|---|
| 2 | 1 | no collision -- passes |
| 3 | 2 | one wins, the other backs off and retries later (`ue1` recovered at tti=3374) -- reaches RRC, PDU unreliable |
| 4 | 3 | none decodes; they stay locked together and retry forever |

Starting all four together is strictly worse, as the model predicts: then all
four collide and none attaches at all.

Two hypotheses were tested and rejected before arriving at this. Raising
`preamble_trans_max` from 7 to 200 changed only the attempt count, because the
UEs were never running out of attempts -- they were failing every one. A stagger
chosen not to be a multiple of the 160 ms RACH retry period behaved identically,
because the stagger is wall-clock and never reaches the UEs' virtual clock at
all.

*Correction: an earlier version of this document attributed the failure to the
gNB merging the preambles onto one C-RNTI. That is wrong -- the gNB issues a
fresh C-RNTI per attempt and the collision is at Msg3.*

### What would have to change

The binding constraint is the stall: a cell cannot serve any UE until every UE
is already transmitting, which both makes incremental attach impossible and
collapses the start stagger that keeps UEs off each other's PRACH occasion.
Letting a destination treat a not-yet-connected source as contributing zero
would fix both -- UEs could join a running cell, and each would start against an
advancing clock and so land on its own occasion. That is a real change to broker
semantics and interacts with the documented rule that the broker never
zero-fills a reply, so it needs design work rather than a patch.

A narrower workaround, if the stall is left alone: stagger the UEs by *virtual*
time rather than wall-clock -- hold each UE until the broker's slot counter has
advanced past the previous UE's PRACH occasion. That does not help while the
clock is fully stopped, so it only becomes useful once a source can be absent.

Until then, **two UEs per cell is the supported multi-user configuration.**

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
