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

Two mechanisms compound, and both were confirmed by measurement rather than
inferred.

**A destination cannot advance until every incoming link has data.** A gNB with
N UEs has N incoming links, and its producer waits for a common input window
across all of them. With four UEs the broker logs
`event=node_stall node=gnb0 phase=input_data` and the producer sits in
`state=wait_data slots=0` for the whole startup, so no uplink reaches the gNB
until the last UE's radio is running. Downlink is unaffected -- the UEs sync to
the cell and transmit RACH into a cell that cannot answer.

**The UEs are phase-locked in the broker's virtual time.** They share one
lock-step clock and all use `preamble_index=0` on `prach_occasion=0`, so the gNB
sees a single merged preamble (`detected_preambles=[{idx=0 ...}]`, one
`tc-rnti`) and contention resolution awards it to one UE. The UEs are started
one at a time precisely to avoid this, but the stall above means the earlier UEs
are still retrying when the later ones arrive.

Measured, four UEs, `preamble_trans_max` raised from 7 to 200:

| Stagger | ue0 | ue1 | ue2 | ue3 |
|---|---|---|---|---|
| 10 s | 200 attempts, no RRC | 200, no RRC | 200, no RRC | 1 attempt, RRC + PDU |
| 9 s | 200 attempts, no RRC | 200, no RRC | 200, no RRC | 1 attempt, RRC + PDU |
| 0 s (simultaneous) | 200, no RRC | 200, no RRC | 200, no RRC | 200, no RRC |

Only the last-started UE attaches, deterministically. Starting them together is
strictly worse -- then none attaches, which is the merged-preamble case in its
pure form. Two hypotheses were tested and rejected: the default
`preamble_trans_max` of 7 (raising it to 200 changed nothing but the attempt
count) and retry-phase aliasing against the 160 ms RACH retry period (a stagger
chosen not to be a multiple of it behaved identically).

With three UEs all three reach RRC on their first attempt, but the PDU session
is unreliable -- one run lost ue0, another lost ue0 and ue1.

### What would have to change

The binding constraint is the first mechanism: a cell cannot serve any UE until
every UE is already transmitting, which makes incremental attach impossible.
Letting a destination treat a not-yet-connected source as contributing zero
would fix it, and would also allow UEs to join and leave a running cell, which
is ordinary behaviour for a multi-UE emulator. That is a real change to broker
semantics and interacts with the documented rule that the broker never
zero-fills a reply, so it needs design work rather than a patch.

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
