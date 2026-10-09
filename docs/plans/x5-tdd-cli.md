# X5 — Two co-channel TDD cells with different patterns: UE-to-UE cross-link interference

Status: 2026-10-01, gate written, four runs on the DGX Spark done (all pass; run 4 shows the CLI). Results at the bottom.

## Why

X3/X4 (`docs/plans/x3-tdd-multi-ue.md`) showed that on one shared TDD carrier a UE<->UE
edge is harmless: the other UE's uplink lands only in the victim's own UL slots
(gNB-TX ∩ UE-TX slots = 0), the DL slots keep their level, attach survives. For the
edge to matter the two UEs must belong to cells whose TDD patterns differ, so that
one UE transmits while the other receives. That is cross-link interference (CLI),
the first physically correct UE-to-UE interference this emulator can show, and it
needs two OCUDU gNBs on the same n78 carrier, each with its own OAI nrUE.

## Files (all new; nothing existing was edited)

| File | Role |
|---|---|
| `scripts/native/render-oai-two-cell-tdd-configs.py` | two gNB yamls from X3's `render_gnb_tdd` + identity/pattern per cell, four-node TDD topology (`carrier: n78` on every port, UE `tx_scale_db` from the OAI constant 1.17e-5, absolute noise floors), nrUE confs / uecap / Open5GS / subscribers via the X3 and multi-UE helpers, `two-cell-tdd-shape.json`. `--self-test`. |
| `scripts/native/run-ocudu-oai-two-cell-tdd.sh` / `-inner.sh` | the gate: X3's OAI multi-UE gate with the two-cell gate's second gNB (started together, camped-PCI check), gNB HARQ/CQI evidence, embedded per-slot numpy analysis of the wire capture. |
| `use_cases/configs/sionna/scenarios/robot_ring/two-cell-tdd-8.json`, `-10.json`, `-12.json` | the scenario (below) with 8 links (control), + ue0<->ue1 (X5), + gnb0<->gnb1 (optional, not run). |
| `tests/integrations/test_oai_two_cell_tdd_renderer.py` | 5 unit tests (self-test, pattern knob and CLI slots, the two cells differ only in identity/ports/pattern, topologies of the three scenarios, shape loader refusals). |

Copied rather than imported, because the originals take exactly one gNB:
`render-sionna-multi-ue-configs.py` `render_topology` / `rx_noise_powers` (same
formulas, N gNBs) and `load_live_shape` (`load_two_cell_shape`). The cell identity
table is `render-multi-gnb-configs.py` CELLS except the PRACH roots: config index
159 is format B4 (L = 139), whose roots live in 0..137, so cell B gets root 70, not
the srsUE gate's 200 (which OCUDU's `--dryrun` accepts silently).

## The two cells

Identical to X3's TDD n78 cell (dl_arfcn 632628 = 3489.42 MHz, 20 MHz, 30 kHz, 51 PRB,
23.04 MS/s, SSB ARFCN 632256, PRACH 159) except:

| | cell A (`gnb0`) | cell B (`gnb1`) |
|---|---|---|
| PCI / gnb_id / ran_node_name | 1 / 411 / gnb0 | 2 / 412 / gnb1 |
| N2/N3 bind | 127.0.0.11 | 127.0.0.12 |
| ZMQ tx/rx | 2000 / 2001 | 2010 / 2011 |
| PRACH root | 1 | 70 |
| TDD pattern | 7D/1S/2U always | knob |

Knob `OCUDU_NATIVE_TDD_PATTERNS`: `same` (default, both 7D2U) or `A;B` with each
pattern `<dl>D<ul>U` on the 10-slot (5 ms) period with one special slot of 6 DL / 4 UL
symbols (dl + ul = 9). OCUDU a1916ed accepts 7D2U, 3D6U and 2D7U (dry run on the
Spark). The nrUE takes the pattern from SIB1 and needs no knob. The runs use
`7D2U;3D6U`:

```
slot          0 1 2 3 4 5 6 7 8 9
cell A 7D2U   D D D D D D D S U U     ue0 receives 0-6 (+ head of 7)
cell B 3D6U   D D D S U U U U U U     ue1 transmits tail of 3, 4-9
CLI ue1->ue0        ^ ^ ^ ^           slots 3(S), 4, 5, 6: 3.5 of ue0's 7.4 DL slots
CLI ue0->ue1  none                    ue0's UL (8, 9) is UL in cell B too
```

SSB and SIB1 (slot 0/1), paging and RA responses stay in slots cell B never uplinks
in, so the victim keeps sync even if its PDSCH in slots 4-6 is destroyed. PRACH
(config 159: subframe 9, slot 19) is UL in both patterns. Both gNBs enable
`metrics: autostart_stdout_metrics` (per-UE DL/UL HARQ ok/nok, CQI, PUSCH SNR once a
second) for the NACK evidence.

In this emulator the two cells are frame-aligned by construction: the broker
relays sample index k of every node in one lock step, both gNBs run the same lower
PHY from their own sample 0, and the per-slot analysis measures the phase of each
gNB's TX (reported as `tdd_phase_offset_gnb1_minus_gnb0_slots`; expected 0).

## Scenario geometry

The task asked for cells clearly separated (>= 15 dB serving/intercell) *and* UEs
close enough that the UE<->UE path is strong. A street canyon (`sionna_simple_test`,
sites 150 m apart, UEs near the midpoint) cannot give both: Sionna dry runs with the
UEs at x = ±10 .. ±40 m give only 3-7 dB serving/intercell separation and a UE<->UE
tap between +6 and −7 dB relative to the serving one. The robot_ring scene can, by
using its 3 m concrete pillars as blockers:

| node | position (m) | note |
|---|---|---|
| gnb0 | (−12, 6, 8) | outside the ring |
| gnb1 | (+12, 6, 8) | mirror |
| ue0 | (−2, 0.8, 0.4) | inside the ring, in the gap between pillar_1 (y −0.4..0.4) and pillar_2 (y 1.2..2) |
| ue1 | (+2, 0.8, 0.4) | mirror, 4 m from ue0 |

Each UE has LOS to its own site; the cross path gnbX->ueY crosses pillar_2 at
x ≈ 0 at ~1.3 m height and is blocked (solver as in X3: depth 3, LOS + specular
reflection + refraction, no diffraction); the UE<->UE path is LOS through the pillar
gap. Sionna dry-run taps at 3489.42 MHz (gain offset 60 dB):

| link | total path power | strongest tap | rays |
|---|---|---|---|
| gnb0->ue0 / gnb1->ue1 (serving, both directions) | −65.4 dB | **−7.3 dB** | 4-5 |
| gnb1->ue0 / gnb0->ue1 (intercell) | −106.6 / −111.6 dB | −47.4 / −51.6 dB | 1-2 |
| ue0->gnb1 / ue1->gnb0 (intercell UL) | −105.4 dB | −47.4 dB | 3 |
| **ue0<->ue1** | −49.8 dB | **+6.0 dB** | 23 |
| gnb0<->gnb1 (12-link file only) | −70.8 dB | −10.9 dB | 2 |

Serving/intercell separation is 40 dB (far above the 15 dB asked for; the price is
that the 8-link "different patterns" run has effectively no CLI path at all, which
makes it a clean control). The UE<->UE tap is 13 dB *above* the serving tap, so with
the UE emitting 7 dB less than the gNB (23 vs 30 dBm after `tx_scale_db` +22.8) ue1's
uplink arrives at ue0 ~6 dB above gnb0's downlink in the overlap slots. Expected
wire levels: gnb0 DL at ue0 = −19.5 − 7.3 = −26.8 dB (slot mean lower, the DL is
mostly empty REs); ue1 UL at ue0 = −26.5 + 6.0 = −20.5 dB when ue1 fills the slot.

## CPU placement (DGX Spark GB10, 10 x X925 5-9,15-19 + 10 x A725 0-4,10-14)

Profile `spark-gb10`: gNB 5-9, broker 15-17, nrUE 18,19 — sized for one gNB and one
UE. X3 gave the two UEs `18,4` and `19,14`. Two gNBs cannot share the five big gNB
cores with that layout, so the gate's default is

| process | cores | kind |
|---|---|---|
| gnb0 | 5,6,0,1,2 | 2 X925 + 3 A725 |
| gnb1 | 7,8,10,11,12 | 2 X925 + 3 A725 (symmetric, same kind of process as gnb0) |
| broker | 15-17 | profile |
| ue0 / ue1 | 18,4 / 19,14 | X3 |
| Sionna bridge, gate, pings | unpinned (core 9 is the only idle X925) | |

Knob `OCUDU_NATIVE_TDD_GNB_CPUS="cpus0;cpus1"`. Whether this is real time is read
from the broker's `rx_starvations`, `node_stall` count and the ue-exec ping RTT in
each run's summary (below).

## Verdict

X3's strict OAI verdict per UE (RRC connected, PDU accept, 3/3 ping + late 5/5, no
SR failure / re-RA / T310 / integrity failure after connect; broker overflows = gaps
= zmq errors = 0; gNBs, Open5GS, nrUEs alive at stop) plus `each_ue_on_its_own_cell`:
the last `Initial sync successful, PCI: N` in each nrUE log must equal its cell's
PCI (ue0 -> 1, ue1 -> 2). Recorded, not gated: per-gNB stdout-metrics totals per
RNTI (dl_ok/dl_nok/ul_ok/ul_nok, CQI, PUSCH SNR), PHY-log PUCCH ack-bit histogram
(1 = ACK, 0 = NACK, 2 = DTX; SR occasions without SR also log as DTX) and PUSCH
crc OK/KO, the nrUE's own `DL Chan: SSB 0 SINR x dB ... CQI y` series (median, min,
p10), the ue-exec ping statistics (loss, RTT), Sionna's last taps, and the per-slot
analysis.

Per-slot analysis (inner script, numpy from `/workspace/sionna-venv`): every 0.5 ms
slot (11520 samples) of every port's `tx_in` / `rx_out` capture is classified by
who transmitted (gNB slot mean > −40 dB, UE slot mean > −70 dB on their own wire);
the TDD phase of each gNB is found from its TX; the table gives every node's median
RX per TX class, and the headline per victim UE: median RX in its own cell's DL slots
with vs without the other UE transmitting, plus "other UE only" and "silent".

## Runs (DGX Spark GB10, tree /workspace/gpuch/xtree, 200 s, capture 1 s after 70 s, `ping -i 0.2` on both UEs)

Filled in from `/workspace/ocudu-spark/results/reports/ocudu-oai-two-cell-tdd/<ts>/attach-summary.json`.

All four runs passed the strict verdict (both UEs on their own cell, RRC + PDU, 3/3 + late
5/5 pings, 0 SR failures / re-RA / T310 / integrity failures, broker overflows = gaps =
zmq errors = 0, every process alive at stop). Chain: `/workspace/gpuch/xt/x5-chain.sh`
(tmux `f`, each run under `flock spark-gate.lock`), consoles `/workspace/gpuch/xt/x5-<label>.out`,
reports `/workspace/ocudu-spark/results/reports/ocudu-oai-two-cell-tdd/<ts>/`
(`attach-summary.json`, `tdd-slot-analysis.json`). Sionna taps in every run were the
dry-run values (serving −7.35 dB, intercell −47.4/−51.6 dB, UE<->UE +6.02 dB, 23 rays).

| # | label / ts | links | patterns | verdict | ue0 RX median in own DL slots: with ue1 TX / without (slots) | ue0 RX: ue1-TX-only slots / silent | gnb0 DL ok/nok (RNTI 4601), gnb CQI med/min | ue0-reported CQI histogram | ue0 ping RTT avg/max ms (loss) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | same8 `20261001T115703Z` | 8 | 7D2U;7D2U | **pass** | n/a / −36.4 dB (0 / 279) | −59.5 / −59.5 | 1914 / 0, 15/15 | 15 ×147 | 27.2 / 41.0 (4/950) |
| 2 | same10 `20261001T120703Z` | 10 (+ue0<->ue1) | 7D2U;7D2U | **pass** | n/a / −36.4 dB (0 / 277) | **−24.4** / −59.5 | 1925 / 0, 15/14 | 15 ×146 | 27.4 / 51.0 (4/951) |
| 3 | diff8 `20261001T121059Z` | 8 | 7D2U;3D6U | **pass** | −44.1 / −36.4 dB (10 / 267) | −59.5 / −59.5 | 1927 / 0, 15/14 | 15 ×146 | 27.4 / 41.8 (4/951) |
| 4 | diff10 `20261001T121443Z` | 10 (+ue0<->ue1) | 7D2U;3D6U | **pass** (attach survived) | **−24.3** / −36.4 dB (11 / 266) | **−32.2** / −59.5 | 1933 / **3**, **12/11** | **0 ×27, 5 ×3**, 15 ×116 | 27.9 / **86.6** (4/951) |

ue1 (cell B) in every run: own DL slots −36.5 dB, 0 slots with ue0 TX, CQI 15 throughout,
gnb1 dl_nok 0 (gnb1 ul_nok 1 / PUSCH crc KO 1 in runs 3 and 4), SINR 37.5 dB — cell A's UL
slots 8-9 are UL in cell B too, so there is no CLI into ue1, as the pattern table predicts.
UE-reported SSB SINR (ue0): 37.7 / 37.65 / 37.7 / 37.5 dB median in runs 1-4, min 35.1-35.8
(34.4 in run 4) — the SSB sits in slot 0, which cell B never uplinks in.

Reading the per-slot classes ("TX" = slot mean above −40 dB on the gNB wire, so only slots
with real DL content count — SSB/SIB1 slots and PDSCH slots — and above −70 dB on the UE wire):

- Same pattern, with the UE<->UE edge (run 2 vs 1): ue1's uplink appears in ue0's receiver
  at **−24.4 dB** (13 dB above the serving downlink's −36.4), but only in slots where gnb0
  does not transmit (gnb0∩ue1 TX = 0), i.e. ue0's own UL slots. DL slots unchanged, CQI 15,
  dl_nok 0. X4's conclusion holds for two frame-aligned cells.
- Different patterns, no UE<->UE edge (run 3): 10 of ue0's 277 DL-content slots coincide with
  ue1 transmitting (cell A's special slot 7, where cell B is already UL); ue0's receive level
  there is **−44.1 dB** — just gnb0's own 6-symbol special slot, nothing from ue1 (the
  intercell path is −47 dB, the gNB RX floor stays −66.5 dB with or without the other gNB
  transmitting). CQI 15, dl_nok 0: a clean control.
- Different patterns, with the UE<->UE edge (run 4, **the X5 result**): in the same class of
  slots ue0 now receives **−24.3 dB** (+20 dB over run 3's −44.1), and in the slots where
  ue1 transmits while gnb0 has no content (cell A's DL slots 4-6 idle, and 8-9) ue0's level
  is **−32.2 dB instead of −59.5** (+27 dB). The victim's receiver sees the other UE's uplink
  at or above its own downlink in 3.5 of its 7.4 DL slots. Consequences: ue0 reported
  **CQI 0 in 27 and CQI 5 in 3 of 146 reports** (CSI-RS in the hit slots), gnb0's stdout
  metrics show ue0's CQI median 12 / min 11 (15 in all other runs), **3 DL HARQ NACKs**
  (0.15 %, 0 elsewhere), and ping RTT spikes to 86.6 ms (41-51 ms elsewhere). Attach
  survived with a clean strict verdict: SSB/SIB1/PDCCH in slots 0-2 are untouched, so sync and
  control hold, and the traffic (pings) is light enough that the scheduler's MCS/CQI reaction
  and 3 retransmissions absorb the damage. With fuller UL from ue1 (iperf) the hit slots
  would be jammed continuously — not run, the gNB stalls under UL traffic (Track D).

Real time: rx_starvations 2 / 3 / 15 / 8, node_stalls 12 in every run (all start-up
`input_data` waits of 4-6 s), tx_pulls 1.51-1.52 M per 200 s, ping RTT avg 22-28 ms on both
UEs (X3's single-cell gate: 26 ms) — the 2 X925 + 3 A725 per gNB placement holds 20 MHz x 2
cells in real time. Both gNBs' first DL slot is capture slot 0 in all runs (phase offset 0):
the cells are frame-aligned by the broker's lock step, as assumed.

Not done: the 12-link file (gNB<->gNB CLI, tap −10.9 dB) and the 2D7U pattern were not run;
UL traffic load under CLI; the CQI-0 reports' effect at higher DL load.

Numbers above were read with `/tmp/x5_summarise.py` (Spark) from each run's `attach-summary.json`; the per-class tables are in `results/logs/ocudu-oai-two-cell-tdd/<ts>/tdd-slot-analysis.log`.


