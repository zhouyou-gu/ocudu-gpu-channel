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

**One UE attaches per distinct PRACH occasion.** Every UE started while the cell
is stalled begins at the same virtual instant, so they share an occasion, share
a preamble, share an RA-RNTI, and end up sharing a C-RNTI. Only the UE whose
arrival ends the stall gets its own.

### The evidence

C-RNTI and first RACH occasion, per UE, across the runs:

| Run | first tti per UE | C-RNTI per UE | PDU sessions |
|---|---|---|---|
| 2 UEs | 334 / **1294** | 0x4601 / **0x4602** | **2 / 2** |
| 3 UEs | 334 / 334 / **1134** | 0x4601 / 0x4601 / **0x4602** | 1 / 3 |
| 4 UEs, single antenna | 334 / 334 / 334 / **1134** | 0x4601 x3 / **0x4602** | 1 / 4 |

The rule is exact: as many UEs complete as there are distinct occasions. Two UEs
pass because N-1 = 1, and one UE on an occasion is not a collision.

### RRC Connected is not proof of attach

This is what made the earlier diagnoses wrong, and it is worth stating plainly:
**srsUE reports `RRC Connected` even when it lost contention resolution.** In the
four-UE single-antenna run all four logged `RRC Connected`, but three of them
were holding C-RNTI 0x4601 -- the same one. They had all decoded the same Msg4
and each concluded it had won. Only the UE with its own C-RNTI ever completed a
PDU session.

So `RRC Connected` counts three UEs that do not exist. Any multi-UE result here
must be judged on **distinct C-RNTIs and completed PDU sessions**, never on the
RRC line.

### Correction: this is not about `ss2_type`

An earlier revision of this document concluded that `ss2_type: ue_dedicated`,
which multi-antenna downlink forces, was the blocker. That was wrong, and it was
wrong because it trusted the RRC line. Reverting `ss2_type` to `common` appeared
to fix four UEs -- 4/4 `RRC Connected` -- but the C-RNTIs show three of those
four were the same UE, and the PDU count stayed at 1 exactly as before.

`ss2_type` changes only how the losers fail. With `ue_dedicated` they are
dropped and honestly keep retrying (200 attempts, no RRC). With `common` they
silently persist on a duplicate C-RNTI and falsely report success. The second is
the more dangerous behaviour, not the better one. **Neither setting changes how
many UEs actually attach, which is governed entirely by distinct occasions.**

Two other rejected hypotheses, both from the same misreading: `preamble_trans_max`
(7 -> 200 changed only the attempt count) and a near/far power spread across the
UEs (no effect).

### Why they share an occasion

A destination cannot advance a slot until every incoming link has data, so a
cell with N UEs produces nothing until the last UE's radio starts -- the broker
logs `event=node_stall node=gnb0 phase=input_data` with the producer in
`state=wait_data slots=0`. The UEs are started one at a time precisely to keep
them apart, but that stagger is in wall-clock seconds while the UEs count frames
in the broker's virtual time, and that clock is not advancing. Every UE started
during the stall therefore begins at tti=334. The last UE's arrival ends the
stall, so it alone starts against a running clock and lands on its own occasion
around tti=1134.

### The fix, and the failed attempt

The fix is to let a destination advance while a source has never connected,
treating it as contributing silence. Then the cell runs from the first UE
onwards, each UE starts against an advancing clock, and the wall-clock stagger
produces real separation. It would also let a UE join a running cell.

This was implemented and reverted. The change excluded never-connected lanes
from the `common = min(available)` window, fed them zeros, held their cursors
still, and deferred cursor co-initialisation until every lane joined. It builds
and `ctest` stays 8/8, but on the live gate it is worse than the problem:

```
before: gnb0 stalls on input_data;      1 of 4 UEs attaches
after:  gnb0 stalls on output_room x72, 0 of 4 UEs even transmit,
        gnb0 TX ring empty
```

Uplink and downlink stopped advancing together. Once the gNB node could process
uplink against silence it ran ahead and filled its own receive rings while the
gNB container -- a lock-step ZMQ radio alternating transmit and receive -- was
still waiting to be pulled; its transmit ring drained, the UEs lost downlink and
never synced.

### Second attempt: an external placeholder radio

The same goal was then pursued without touching the broker at all. If every
unlaunched UE's transmit port is held open by a placeholder source, the cell
never stalls, so each real UE starts against an advancing clock and lands on its
own occasion. The placeholder was made *paced* to the cell sample rate and
*silent*, so that it behaves like a radio powered on and transmitting nothing
(`ocudu-zmq-source --sample-rate-hz --silent`, added for this).

It fails the same way, and the gNB says why:

| Run | gNB `Real-time failure in RF` lines | Outcome |
|---|---|---|
| 2×1 single-UE gate | **0** | passes |
| 2×1 two-UE gate | **0** | passes |
| four-UE, single antenna, no placeholders | **0** | 1 of 4 attaches |
| four-UE with placeholders, unpaced | **12,161,310** | 0 of 4 even transmit |
| four-UE with placeholders, paced + silent | **12,168,638** | 0 of 4 even transmit |

Pacing changed nothing. The gNB receives uplink from the instant the cell comes
up, before it is ready to consume it, overflows continuously, and then spends
the run emitting some 67,000 log lines per second, which is what actually wedges
it. Every configuration that works has exactly zero of these.

### Third attempt: answer cold receive requests with silence

The loop analysis says the deadlock is that a lock-step radio blocks in receive
until answered, so it never reaches transmit. The narrowest fix for that is in
the REP worker rather than the producer: while a port has **never** delivered
real IQ, answer a receive request with silence instead of holding it. Once the
first real row goes out the flag latches and the strict rule applies for the
rest of the run, so an established stream can never be zero-filled.

It builds, `ctest` stays 8/8, and on the live gate it fails the same way:
9,407,829 RF overflow lines, no UE transmits.

The reason completes the picture. The grace period was 20 ms, so each cold reply
delivered 1 ms of samples after a 20 ms wait -- the gNB fell twenty times behind
wall-clock and reported real-time failure. Answering *faster* is not the fix
either: with no data to pace it, the broker would answer instantly and the gNB
would free-run ahead of real time, which is the failure mode of the first two
attempts. The correct cold-start behaviour is to emit silence at exactly the
sample rate, which is precisely what the node producer's throttle already does
when it has real inputs -- and which attempt one showed is not sufficient on its
own either.

### What this says

Three independent attempts -- the producer's input window, an external
placeholder radio, and the REP worker's reply policy -- fail identically. Taken
together they bracket the problem:

| Attempt | Where | Result |
|---|---|---|
| Cold source contributes silence | producer input window | gNB stalls on output_room, TX ring empties |
| Paced silent placeholder radio | outside the broker entirely | 12.1M RF overflow |
| Cold receive answered with silence | REP reply policy | 9.4M RF overflow |

Answer too slowly and the radio reports real-time failure; answer too quickly
and it runs ahead of its own clock. There is no setting of a timeout that is
both. What a lock-step radio needs is silence delivered *at the sample rate*,
from a source that is itself paced by the same clock as the rest of the relay --
in other words, an absent peer has to be modelled as a real participant that
transmits nothing, not as a special case in whichever component happened to
notice it was missing.

That is a startup-contract change, and it belongs in one place: a node whose
peer has not connected should be driven by the same throttle that paces every
other node, emitting silence into the loop at real time, with the destination's
own consumption -- not output-ring room -- as the back-pressure signal. None of
the three attempts above put it there, which is why all three failed in
different components for the same underlying reason.



Two independent approaches -- one inside the broker, one entirely outside it --
produce the same failure: the moment the gNB's uplink can advance without a real
lock-step peer on every link, the radio's timing breaks.

So the input-window rule is not merely conservative, and it is not separable
from the stall. In this design the broker's REQ/REP exchange *is* the radio's
clock, and a lock-step radio requires every one of its peers to be present and
driving that clock from the start. Admitting absent peers is not a patch to the
window calculation; it needs a startup contract in which a node can advance
against declared-absent peers *without* the destination radio seeing uplink it
has not asked for -- gating on the destination's own consumption rather than on
output room. That is a design change to the pacing model, and it should be
designed rather than attempted incrementally, which is what these two attempts
were.

The paced/silent placeholder options are kept because they are a genuine
improvement to the test source, and `OCUDU_MUE_PLACEHOLDERS` is kept as an
opt-in for anyone continuing this, but it defaults to off because it breaks the
gates.

Until that exists, **two UEs per cell is the supported multi-user configuration**,
and it is verified on distinct C-RNTIs and completed PDU sessions rather than on
the RRC line.

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
