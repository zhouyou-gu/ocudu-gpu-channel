> Historical report migrated from `docs/rank1-feasibility-report.en.html` at `58d3156`; original findings retain their recorded scope.

Engineering results · feasibility evidence

# Rank-1 MISO/SIMO Implementation Results

The approved target of the integration assessment (`mimo-integration-report.html`, 2026-08-16) — DL 2×1 (then 4×1) MISO plus UL 1×2 (then 1×4) SIMO with srsUE kept at one antenna — has been **implemented and verified live, end to end**, on a real OCUDU gNB ↔ GPU channel broker ↔ srsUE 5G path. **All Sionna RT items are excluded as instructed** (owned by another team member; merged later).

Workspace **ocudu-gpu-channel-rank1** · branch **rank1-miso-simo** Reported **2026-08-17** Every number measured · reproducible from the evidence index (§08) 
(rank1-feasibility-s0)=
 00 · SUMMARY

## One-paragraph summary

In both the two-port and the four-port configuration, a real srsUE completes attach + PDU session + user-plane ping (60 packets, 0% loss); **within the same gate run** a wire capture is recomputed as y = H·x and judged against the declared channel vectors; and GPU processing stays inside the 1 ms slot budget. The live multi-port proof the assessment listed as a "Not yet demonstrated" delivery gate is closed, and the four integration constraints discovered along the way are each root-caused with the fix baked into the fixtures.

2 / 2 gates pass2×1 and 4×1 live gates (attach + PDU + ping + matrix judgement), reproduced on a second host and harness ≤ 4.6e-05worst live y=Hx error (all UL rows; tolerance 1e-04) 12.9 µs4×1 GPU kernel p50 (1 ms slot budget) 0.01 dBoracle-precoding measurement vs prediction (MRT / duplication / null sweep) 
(rank1-feasibility-s1)=
 01 · FEASIBILITY VERDICT

## The assessment's verdict questions, answered today

The assessment (§01) judged the public remote snapshot `a801155`. The work since then changes the answers below — and each answer is locked behind a repeatable gate.

| Question | Answer at assessment time | Answer today · evidence |
|----|----|----|
| Can this project emulate a multi-antenna channel? | No — scalar/SISO per node | Yes — RadioNode multi-port model, per-direction row/column vectors (`fixed_mimo`), live y=Hx ≤ 4.6e-05 |
| Does 2×1 / 1×2 rank-1 run live? | Not yet demonstrated (delivery gate) | UL 1×2 yes (multi-branch) · DL 2×1 procedure only (single branch) — `ocudu-rank1-2x1-smoke.sh` **result=pass**, matrix judgement passed. Both gNB receive ports take their own branch and are scored per row. srsRAN radiates on port 0 only, so the live DL row is a single-branch check; DL multi-branch evidence comes from the synthetic gates and the oracle experiment (see the residual limits). |
| Does it extend to 4×1 / 1×4? | Planned (gated on the two-port pass) | UL 1×4 yes (all four branches) · DL 4×1 procedure only (single branch) — `ocudu-rank1-4x1-smoke.sh` **result=pass**, all four UL rows pass y=Hx. DL transmit ports 1, 2 and 3 are declared silent, so the live downlink judgement reduces to the scalar y = h0·x0. |
| Does srsUE stay rank-1? | Requirement | Kept — `nof_antennas = 1` unchanged, the 1×1 regression gate stays green, and every result is described as rank-1 MISO/SIMO only |

**Operational definition of feasibility** — "does the real OCUDU stack and the real srsUE complete the standard procedure chain (RA → RRC → NAS registration → PDU → user plane) through the multi-port channel vectors we declare?" By that definition the answer is **Yes for both two and four ports.** 
(rank1-feasibility-s2)=
 02 · LINEAGE

## From the pre-MIMO code to today

BASELINE — pre-MIMO (public snapshot line a801155)

#### One stream per node, one scalar channel per directed edge

- Live 1×1 OCUDU↔srsUE attach, TR 38.901 TDL-A..E, GPU kernel (183× speedup), runtime control plane
- No antenna-port dimension — the point where the assessment's "No" verdict was earned

▼ M0–M5 — MIMO rebuild (parent tree)

#### The RadioNode overlay: alignment as a structural invariant, not a protocol

- One producer thread per node exclusively owns the common sample window — no second actor exists that could pick a different window for a sibling port
- Port declaration order in `radio_nodes` IS the matrix index · sparse `fixed_mimo` coefficients · spatial correlation / coherent LOS
- Atomic per-physical-link runtime control (a partially updated port vector is unrepresentable)
- Live multi-port transport against a real 2T2R gNB, up to and including wire-capture y=Hx matrix verification

▼ R0–R3 — the rank1 fork (this tree, aligned to the assessment minus Sionna)

#### Asymmetric (N×1 / 1×N) configurations and live srsUE integration

- R0 asymmetric topology → R1 deterministic-channel proofs (phase sweep, branch isolation) → R2 live 2×1/1×2 → R3 4×1/1×4 plus a root-cause investigation
- Then: matrix judgement folded into the gates · labelled performance envelopes · the oracle-precoding study

**Why this path favoured feasibility** — the data-model changes the assessment's §07 required (endpoint arrays per port, per-direction vector channels, a grouped slot barrier, atomic vector activation) are structurally the same things M0–M5 had already built. The rank1 work was therefore integration and verification, not redesign: **outside one phase-sweep unit test, zero emulator-core C++ changed.** 
(rank1-feasibility-s3)=
 03 · REQUIREMENTS

## Requirement coverage (against the controlling brief)

| Brief requirement | How it is met · evidence |
|----|----|
| OCUDU gNB + srsUE 5G through the real-time GPU ZMQ emulator | R2/R3 live gates pass; strict counters all zero; zero gNB RF real-time failures |
| Keep srsUE at one TX/RX stream, one layer | `nof_antennas = 1` unchanged — the UE config renders byte-identical to the 1×1 regression gate |
| gNB-side array/diversity benefit with a rank-1 UE | UL: the gNB genuinely equalises 2/4 receive ports (per-row y=Hx). DL: the oracle study measures the +0.54 dB MRT array gain live |
| Validate two ports before gating four | R2 → R3 sequential gates, each passing |
| Study fixed / oracle-selected rank-1 precoding and the effective channel | Four-weight oracle sweep, live, matching prediction to 0.01 dB (§05) |
| Prove coefficients and port alignment with synthetic peers before live srsUE | R0/R1 synthetic proofs precede R2/R3 live runs |
| Preserve the 1×1 path as a regression test | `run-ocudu-legacy-1x1.sh` untouched and green alongside |
| Precoding/beam weights stay in OCUDU/RU; propagation/fading/noise/superposition stay here | Honoured — every emulator-supplied weight is explicitly labelled "oracle" |
| One logical slot boundary across gNB ports; atomic channel updates | The producer's single window (structural) plus M4's atomic control — the live y=Hx pass is itself the measured proof of alignment |
| Zero starvation-class integrity errors · CPU/CUDA correctness · fully labelled performance | All gates: 0 overflow / 0 gap / 0 ZMQ errors · ctest 8/8 on both trees · the fully labelled table in §05 |

**What is NOT claimed (per the brief's writing rules)** — rank\>1 SU-MIMO (a separate workstream in the parent tree), UE receive diversity or beam steering, automatic closed-loop PMI beam control, same-PRB MU-MIMO, or the phrase "end-to-end 4×4 MIMO". Duplication is never called diversity — the oracle study in fact proves duplication is **1.24 dB worse than MRT** (§05). 
(rank1-feasibility-s4)=
 04 · LIVE GATES

## Feasibility evidence I — live end-to-end gates

The primary evidence is "does the real stack actually complete". Both gates are single repeatable commands, and each judges four things **within one run**:

① Procedure completion

#### RA → RRC → NAS registration → PDU → ping

User-plane ICMP: 60 packets, 0% loss required

② Transport integrity

#### Strict counters = 0

Zero overflow / sequence gap / ZMQ errors · zero gNB "Real-time failure in RF"

③ Channel content

#### y = H·x recomputation

A 500 ms wire capture flushed by the broker at shutdown is recomputed by an independent checker against the DECLARED topology H — it grades the computation, not the traffic

④ Provenance

#### Reproducibility

Source-commit pins, binary/config SHA-256, and the channel source manifest preserved as JSON

| Gate | Run ID | Result | Live y=Hx (tolerance 1e-04) |
|----|----|----|----|
| **2×1 DL + 1×2 UL** | 20260817T091805Z | pass | UL rows ≤ 2.2e-05 · DL row 3.7e-08 — live TX branches: UL **2/2**, DL **1/2** |
| **4×1 DL + 1×4 UL** | 20260817T091856Z | pass | UL rows = 4.6 / 2.2 / 1.7 / 2.8 ×e-05 — live TX branches: UL **4/4**, DL **1/4** |
| **2×1 reproduced** (container harness, 2026-08-19) | different host · different 5GC | pass | UL rows 4.62 / 2.12 ×e-05 · DL row 4.85e-08 |
| **4×1 reproduced** (container harness, 2026-08-19) | different host · different 5GC | pass | UL rows 4.61 / 2.16 / 1.59 / 2.68 ×e-05 · DL row 5.31e-08 |
| 1×1 srsUE regression | same day | pass | legacy path unregressed |
| Standing GPU sequence | same day | 9/9 pass | synthetic relay · AWGN · graph · two-cell · TDL-A, etc. |

Per-row UL RMS matches each declared coefficient magnitude \|h<sub>r</sub>\| to a relative error of ~6×10⁻⁷ (the fp32 floor) — **each of the gNB's 2/4 receive ports receives its own independent branch exactly, and OCUDU genuinely combines them in decoding.** That is the live proof of what the assessment called "the strongest end-to-end diversity target". 
(rank1-feasibility-s5)=
 05 · QUANTITATIVE EVIDENCE

## Feasibility evidence II — the numbers

### Channel accuracy · analytic closure

- Four-port **synthetic** capture: all four UL rows max\|y−Hx\| ≤ 1.3e-07 · DL 1×4 row 1.24e-07 · off-diagonal share 1.18 (all four sources contributing)
- Coherent phase sweep (a mutation-verified unit test — the same waveform into both TX lanes): φ=0 → 2× amplitude, φ=π → 0, φ=π/2 → 1+j — exact
- Live-broker branch isolation: tx1-only received power measured 0.0280165 vs 0.02802 predicted (**0.05%**) — and once the synthetic peer's deterministic marker correlation is modelled, the coherent-sum power reproduces the prediction to four decimal places

### Real-time budget (fully labelled)

**Method correction.** The previous edition computed these percentiles from the 1 Hz heartbeat, which publishes only the last completed slot — 18 samples out of the ~20,000 slots a 20 s run processes, so its "p99" was really the maximum of 18 points. The broker now accumulates *every* slot into a 5 µs-bucket histogram and emits `event=process_latency_summary` at shutdown; the figures below are whole-run percentiles. The old numbers were wrong in both directions: 2×1 understated the tail (p99 131.9 → 205 µs) and 4×1 reported a single outlier as p99 (619.1 → 285 µs).

Intel Core Ultra 9 285K · one RTX 5090 · 23.04 MS/s · batch 23040 (= 1 ms slot) · CUDA backend · fixed_mimo (single tap) + TDL chain · live container gate, 60 s run · every slot recorded

| Config | GPU h2d / kernel / d2h p50 (µs) | Node process p50 / p99 (µs) | Share of the 1 ms slot (p99) |
|----|----|----|----|
| **2×1** gnb0 (n=57,753 slots) | kernel 10.6 | 80 / 205 (p99.9 340) | 21% at p99 |
| **4×1** gnb0 (n=54,439 slots) | kernel 12.9 | 115 / 285 (p99.9 675) | 31% at p99 |

Both configurations stay inside the 1 ms slot budget through p99.9. **One honest caveat:** the observed maximum in both lands in the 5 ms overflow bucket. With p99.9 at 340 µs (2×1) and 675 µs (4×1) those slots are fewer than 0.1% of the run and appear to sit at run start and teardown, but the previous edition's claim that the budget holds "observed maxima included" no longer stands. Attributing that outlier is an open item, as is re-measuring before any 8-port+ extension.

### The oracle-precoding study — "gain exists only where the phases match", proven live

Precoding belongs to the gNB/RU, but srsRAN offers no injection knob — so the emulator declares the **effective** scalar c = h·w of the physical row h = \[0.80+0.10j, 0.25−0.15j\] under a weight chosen by the experimenter **with channel knowledge (an oracle)**. Since amplitude is not gain on a noiseless channel, a fixed −30 dB AWGN floor makes \|c\|² an actual SNR. Each case is a full live attach run:

| Oracle weight | Predicted (vs MRT) | Measured | Live outcome |
|----|----|----|----|
| **MRT** conj(h)/‖h‖ (matched) | 0 dB | reference | attach + PDU + ping |
| port0-only \[1, 0\] | −0.53 dB | **−0.54 dB** | attach + PDU + ping |
| naive duplication \[1, 1\]/√2 | −1.24 dB | **−1.24 dB** | attach + PDU + ping |
| anti-matched deep null | −38.7 dB | (cell gone) | cell search fails — as predicted |

Absolute received power matches \|h·w\|²·P<sub>tx</sub>+noise at ratio 1.000 in all four cases. This is a **live measurement of the 2×1 MRT array gain (+0.54 dB)**, a quantitative proof that duplication is not diversity, and a demonstration of anti-matched self-cancellation. The weights are oracle-chosen; no closed-loop PMI is implied.

(rank1-feasibility-s6)=
 06 · INTEGRATION CONSTRAINTS

## Feasibility evidence III — root-caused constraints and their fixes

The most valuable part of a feasibility result is "**under which conditions** does it work". The four constraints discovered during live integration are each root-caused, and each fix is locked into the fixtures with an inline rationale plus a renderer invariant.

| \# | Symptom | Root cause (evidence) | Fix (where it lives) |
|----|----|----|----|
| 1 | gNB refuses to start at 2T2R with the conventional srsUE PDCCH override | OCUDU's validator forbids fallback DCI (SS#2) with \>1 DL antenna (`du_cell_config_validation.cpp:290`) | `ss2_type: ue_dedicated` + `dci_format_0_1_and_1_1: true` — and a 1×1 control run **proved srsUE actually supports DCI 0_1/1_1**, overturning the received wisdom |
| 2 | Connection collapses into a release/re-RA loop right after RRC at 2T2R | The srsUE PoC cannot apply a 2-port NZP-CSI-RS (FD-CDM2) resource — its whole connection-setup procedure dies (UE log: "Resource mapping is invalid or not implemented") | `csi_rs_enabled: false` + `nof_cell_csi_res: 0` |
| 3 | gNB refuses to start with multiple DL antennas | OCUDU rejects MAC pcap combined with \>1 DL antenna as an invalid configuration | `pcap.mac_enable: disable` |
| 4 | Registration fails probabilistically (bimodal) at UL 4R | With CSI off, the gNB's UL link adaptation trusts its own SNR estimate — which saturates at ~11 dB on the noiseless channel while the residual distortion is not AWGN — and keeps scheduling 64QAM (rate 0.93), producing a **standing ~20% PUSCH KO rate** (identical in passing runs). A run dies exactly when a consecutive-KO burst lands on the registration-critical window: the bimodality is survival probability, not a state | `pusch.max_ue_mcs: 9` — full 4T4R then attaches **3/3 with zero KO** |

**The investigation method is itself the credibility** — for \#4, the channel/broker (wire y=Hx exact even in failing runs), ring sizing, UE config application, resync events, and the tx/rx stream-origin phase were **each eliminated by direct measurement**; and the headline "failure signal" `epre=-inf` turned out to be the **normal DTX log** of SR occasions where the UE had nothing to request (cross-checked: 308 SRs transmitted ↔ 308 detected; all 841 granted PUSCH occasions in 1:1 correspondence). The emulator was innocent even in the failing runs — every constraint sits with the integration partners (stack policy / PoC UE scope), and those boundaries are now documented knowledge.

### Honest residual limits

- **gNB ports ≥ 1 never radiate on the DL in this configuration** — srsRAN puts SSB/common channels on port 0 only and precodes rank-1 PDSCH as \[1,0,…\] (measured on the wire). The DL multi-branch coherent content is therefore proven by the synthetic gates (R0/R1) and the oracle effective-channel study, and the in-gate matrix judgement **declares** this fact via `--allow-silent-source` (a recorded statement of a measured fact, not a waived check).
- `max_ue_mcs: 9` is the partner setting of CSI-off and caps UL throughput — this workstream makes no throughput claims, so it stays inside the stated result boundary.
- The 4-port real-time p99 margin (§05) is a re-measurement item before any 8-port+ extension.

(rank1-feasibility-s7)=
 07 · NEXT

## Next steps

- **Sionna RT merge interface** (when its owner joins): this tree's wire-capture / matrix-judgement infrastructure is directly reusable as the scoring tool for the record/replay validation gates, and the per-direction vector channels with atomic activation are the receiver-side structure the coefficient-horizon contract requires.
- Re-measure real-time margins before extending the gNB side past four ports; add spatial correlation / CDL (assets already exist in the parent tree) if a beam/diversity study requires it.
- Rank\>1 continues separately in the parent tree (`ocudu-gpu-channel-mimo-claude`, OAI-nrUE workstream) — outside this tree's scope.

(rank1-feasibility-s8)=
 08 · EVIDENCE INDEX

## Evidence index

| Evidence | Location |
|----|----|
| Live gates (re-runnable) | scripts/remote/ocudu-rank1-2x1-smoke.sh · ocudu-rank1-4x1-smoke.sh · ocudu-attach-smoke.sh (supported, reproducible) — scripts/native/run-ocudu-rank1-\*.sh records the original runs but cannot be provisioned from this repository |
| Gate artefacts (summary JSON · matrix report · logs · source pins) | ~/ocudu-native-workspace/results/{reports,logs}/rank1-2x1/20260817T091805Z · rank1-4x1/20260817T091856Z |
| Fixtures (constraint rationale inline) | use_cases/configs/ran/ocudu/native/gnb_zmq_b210_fdd\_{2t2r,4t4r}\_rank1_srsue.yaml · use_cases/configs/topologies/ocudu_native/topology.ocudu.rank1-{2x1,4x1}.cuda.yaml · topology.ocudu.rank1-2x1-oracle-mrt.cuda.yaml |
| Independent matrix checker (reads H from the topology, trusts nothing the broker emits) | scripts/native/verify-mimo-matrix-capture.py |
| Phase-sweep unit test (mutation-verified) | tests/core/test_processing.cpp — R1 section |
| Full work & investigation narrative (incl. eliminated hypotheses) | AGENT_PROGRESS.md — "Rank-1 Workstream" section |
| Roadmap · status · measurement labels | [RANK1_MILESTONES.md (historical)](https://github.com/zhouyou-gu/ocudu-gpu-channel/blob/5ffd73ea4afa2295b9a588b0b03a411329e81d9b/RANK1_MILESTONES.md) |
| Commit history | branch rank1-miso-simo, grafted onto upstream 216a28b (fork point 34f669e · R0–R2 06d27d1 · R3 root cause 4d98e2a · gate integration e31fe51 · oracle e630bba). These are the published hashes; the pre-graft private-tree hashes do not resolve on this branch. |

**Assessment boundary.** Every claim in this document is reproducible from the artefacts indexed above; nothing absent from that index is claimed. Rank\>1, UE receive beamforming, closed-loop PMI, MU-MIMO, and end-to-end 4×4 MIMO are not results of this workstream. Sionna RT integration is excluded as instructed and will be merged with its owner's workstream later.\
Generated from `docs/rank1-feasibility-report.md` · 2026-08-17
