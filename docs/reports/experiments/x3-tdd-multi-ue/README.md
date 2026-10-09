# X3/X4 — TDD n78 multi-UE gate with OAI nrUEs, and the UE<->UE edge where it is physical

> Dated evidence, preserved from `docs/plans/x3-tdd-multi-ue.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../../getting_started/status.md).


Status: 2026-10-01, results section filled in as the Spark runs land (see bottom).

## Why

CROSSTALK_MILESTONES.md X0–X2 removed the UE<->UE edge from the FDD fixtures (no
such path exists on band 3) and taught the broker to refuse an edge between
ports with different carrier labels. X3/X4 are the other half: on a **shared TDD
carrier** a UE's uplink does land in the other UE's receiver, so the edge is
physical and the broker admits it (every port `carrier: n78`). The claim to
verify is that with the per-device transmit scale right, that edge does not
break attach — its energy falls into the victim's UL slots, where the victim is
not listening, and the DL slots keep their level. srsUE is 15 kHz FDD only, so
this needs the OAI nrUE and a new gate.

## Files (all new; nothing existing was edited)

| File | Role |
|---|---|
| `scripts/native/render-oai-multi-ue-configs.py` | gNB yaml (TDD n78 20 MHz), two nrUE confs, Open5GS/subscriber via the multi-UE helpers, broker topology via the Sionna renderer's `render_topology` + TDD labels + veth endpoints, uecap, radio args, `oai-multi-ue-shape.json` metadata. `--self-test`. |
| `scripts/native/run-ocudu-oai-multi-ue.sh` / `-inner.sh` | The gate (clone of the srsUE multi-UE gate's structure with the OAI 1x1 gate's UE launch, once per UE). |
| `use_cases/configs/ran/oai/nrue_zmq_multi_ue.conf.in` | nrUE template, `@UE_IMSI@` per UE. |
| `use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk-tdd.json` | robot-ring-walk with 4 `sionna_rt` links (X3 baseline). |
| `use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk-tdd-ue2ue.json` | + `ue0->ue1`, `ue1->ue0` `crosstalk` (X4). |
| `scripts/native/wire-capture-tdd-slots.py` | per-TDD-slot power of a broker wire capture (phase from the gNB TX). |
| `tests/integrations/test_oai_multi_ue_renderer.py` | 6 unit tests (self-test, cell, labels/endpoints/scale, UL power override, IMSIs, uecap). |

## The TDD cell

| Parameter | Value | Why |
|---|---|---|
| band / duplex | n78 TDD | the one OAI nrUE + OCUDU combination that runs 30 kHz here (S8/S10 100 MHz runs) |
| dl_arfcn | 632628 (3489.42 MHz) | OCUDU's own `gnb_rf_b200_tdd_n78_20mhz.yml`; even ARFCN = on the 30 kHz raster |
| bandwidth / SCS / PRB | 20 MHz / 30 kHz / 51 | |
| sample rate | 23.04 MS/s | 768-point FFT (OAI `-E`, 3/4 of 1024); OCUDU's 20 MHz n78 example runs at 23.04 too → broker `batch_samples` stays 23040 |
| SSB | ARFCN 632256, offsetToPointA 0, k_SSB 0, CORESET#0 index 0 | what the audited gNB derives and prints ("SSB derived parameters"); nrUE `--ssb 0` |
| TDD pattern | `dl_ul_tx_period 10, nof_dl_slots 7, nof_dl_symbols 6, nof_ul_slots 2, nof_ul_symbols 4` | OCUDU's ran550 n78 example (7D + S(6:4) + 2U per 5 ms) |
| PRACH | `prach_config_index 159` | OCUDU's TDD n78 examples |
| PDCCH | fixture's `dedicated` (srsUE-only) and `common` (15 kHz table) blocks removed | OAI 1x1 gate's adaptation + the bandwidth renderer's 100 MHz edit |
| nrUE radio args | `-E -r 51 --numerology 1 --band 78 -C 3489420000 --ssb 0` | no `--CO` (TDD) |
| uecap | stock `uecap_ports1.xml` (it already lists 30 kHz / 20 MHz DL and UL per-CC sets; the renderer adds the UL one only if missing) | |
| Sionna | `--carrier-frequency-hz 3489420000` (one frequency for every direction) | run_bridge.py's TDD override; the scenario JSON has no frequency field |

## Transmit scale and noise floor

Same formula as X1 (`render-sionna-multi-ue-configs.py`): `ue_tx_scale_db =
(23 − 30) − 10·log10(TX_POWER_UL / TX_POWER_DL)`, gNB floor from the scaled UL.
The OAI nrUE's wire level was calibrated on the OAI 1x1 gate
(`oai-1x1/20261001T110037Z`, 3 s window over the gate's pings: six 14-symbol
64-PRB QPSK PUSCH at −42 dB plus PUCCH at −56..−57 dB): **1.17e-5 (−49.3 dB)**
against the gNB's 1.12e-2 (−19.5 dB). OAI emits every uplink channel at a fixed
digital amplitude per RE (ZMQ module scales int16 by 1/32767), so the level
follows the allocation width; the traffic-weighted value is what
`render-oai-1x1-configs.py` uses too. It is 94 dB below srsUE's 3.0e4, so the
OAI UE port gets a **+22.8 dB gain** (`tx_scale_db: 22.810`) where srsUE got
−71.3 dB; the gNB floor is 2.23e-7 at 40 dB reference SNR.
`OCUDU_NATIVE_SIONNA_TX_POWER_UL` overrides the level per run. The first two
Spark runs below used the earlier PUCCH-only provisional level 2.0e-6
(+30.5 dB); the re-run with the calibrated constant is the reference.

## Verdict (strict, per UE, from the nrUE console log)

Counted after the first `State = NR_RRC_CONNECTED`:

| field | OAI token | srsUE equivalent |
|---|---|---|
| rrc_connected | `State = NR_RRC_CONNECTED` | `RRC Connected` |
| pdu_session_established | `Received PDU Session Establishment Accept` | `PDU Session Establishment successful` |
| sr_failures | `SR not served! SR counter N reached sr_MaxTransmissions` (nr_ue_procedures.c) | `Scheduling request failed` |
| reattach_attempts | `Triggering new RA procedure for UE with RNTI` + any later `Initialization of 4-Step CBRA procedure` | `Random Access Transmission` after connected |
| rlf_count | `Timer T310 expired` (rrc_timers_and_constants.c), `Radio Link Failure`, `RLF` | same |
| integrity_failures | `Integrity check failure` (two UEs merged on one C-RNTI) | same |
| ping | `ping -I oaitun_ue1 -c 3` right after attach **and** `-c 5` 30 s before the broker stops, all answered | 3/3 |
| recorded, not gated | `Resynchronizing RX by`, `RA procedure succeeded`, `UE synchronized!`, broker `rx_starvations`, heartbeats, Sionna updates | |

Plus the broker's `tx_queue_overflows = tx_sequence_gaps = zmq_errors = 0`,
broker exit 0, gNB / Open5GS / both nrUEs alive at broker stop.

## CPU placement (DGX Spark GB10)

Profile `spark-gb10` pins gNB 5-9, broker 15-17, nrUE 18,19 — sized for ONE
OAI UE, and the ten X925 cores are then all taken. The gate gives each UE one
profile core plus one A725 core: **ue0 `18,4`, ue1 `19,14`** (symmetric, so the
two UEs are the same kind of process for the UE<->UE comparison; 20 MHz is the
lightest cell). Knob `OCUDU_NATIVE_OAI_MUE_UE_CPUS="cpus0;cpus1"`.

## Runs (DGX Spark GB10, tree /workspace/gpuch/xtree, strict verdict)

| Run | Scenario | Links | UE scale | Verdict | Notes |
|---|---|---|---|---|---|
| `ocudu-oai-multi-ue/20261001T110320Z` | robot-ring-walk-tdd (ue1 walks the shadow side) | 4 | +30.5 | **fail**: ue0 clean; ue1 rrc/pdu/ping ok but 51 re-RA, 17 T310, 1 SR failure | The nrUE has no AGC (fixed rx gain −12 dB) and loses sync in the ~25 dB pillar shadow; srsUE survived it. TDD cell and both attaches work. |
| `…/20261001T110816Z` (los4) | robot-ring-walk-tdd-los (both UEs on the LOS side, 1.5 m apart) | 4 | +30.5 | **pass**, both clean, ping 3/3 + late 5/5 | X3 baseline. |
| `…/20261001T111238Z` (los6) | robot-ring-walk-tdd-los-ue2ue | 6 (+ue0↔ue1) | +30.5 | **pass**, both clean | X4. UE↔UE tap med +4.1 dB (max +12.7), i.e. 10 dB *above* the serving −6 dB link. |
| `…/20261001T111857Z` (los6b) | same | 6 | **+22.8** | **pass**, both clean, starvation 4 | X4 with the calibrated constant. |

Per-slot evidence (0.5 ms slots, 1 s capture after 70 s, `numpy` over the
`wire-capture/*.cf32`; gNB-TX slots = slot mean > −40 dB, UE-TX slots = slot
mean > −67 dB on the UE's own wire):

| Victim ue0 RX median | los4 (4 links) | los6 (+30.5) | los6b (+22.8) |
|---|---|---|---|
| slots where only ue1 transmits (UL slots) | −59.5 dB (noise floor) | **−29.2 dB** | **−36.9 dB** |
| slots where the gNB transmits (DL slots) | −33.6 dB | −33.6 dB | −33.6 dB |
| silent slots | −59.5 dB | −59.5 dB | −59.5 dB |
| gNB-TX ∩ ue1-TX slots | 0 | 0 | 0 |

The other UE's uplink lands in the victim's receive stream only in the UL
slots, where the victim is itself transmitting and not decoding; the DL slots
are untouched, and the UE stays attached with the UE↔UE edge present — the
behaviour a shared TDD carrier should have. (The 7.7 dB difference between
los6 and los6b is exactly the scale change.) For UE↔UE interference to matter
the two UEs must be in cells with different TDD patterns (X5, open).

Open: the nrUE's sync loss in deep shadow (no AGC) limits scenarios to LOS
geometries unless `OCUDU_NATIVE_OAI_UE_RX_GAIN_DB` is raised or the shadow
softened; the gate always runs two UEs; the Web UI is off by default.
