> Historical report migrated from `docs/mimo-integration-report.html` at `58d3156`; original findings retain their recorded scope.

Engineering assessment · research snapshot 16 August 2026

# Rank-1 MISO/SIMO integration for ocudu-gpu-channel

What OCUDU’s four-port gNB can do with one-port srsUE 5G, and how buffered Sionna RT channel state can drive validated 2×1/4×1 downlink MISO and 1×2/1×4 uplink SIMO in the GPU emulator.

Source-backed Local project: `a801155` CUDA fork: `nvcuda_accel_02 @ b708b13` srsRAN_4G: `release_25_10` Sionna RT: `2.0.1` 
(mimo-integration-assessment-brief)=
 00 · User-owned scope

## Project goal, intentions and requirements

This is the controlling brief for the report. Later recommendations, diagrams, roadmap items and claims must remain consistent with it unless the user explicitly revises this section.

Goal

#### Demonstrate useful rank-1 multi-antenna operation

- Use an OCUDU gNB and srsUE 5G through the real-time GPU ZMQ channel emulator.
- Keep srsUE as a one-transmit-stream, one-receive-stream, one-layer NR UE.
- Use multiple OCUDU antenna ports to obtain gNB-side array or diversity benefits without representing the UE as rank greater than one.
- Export site-specific, antenna-indexed Sionna RT channel state into the GPU emulator without placing ray tracing in the real-time IQ serve path.
- Produce evidence-backed end-to-end results from attach, PDU session, traffic, PHY metrics and channel telemetry.

Intended experiments

#### Start with two gNB ports, then gate four

- Downlink: first validate 2×1 rank-1 MISO, then extend to 4×1.
- Uplink: first validate 1×2 rank-1 SIMO receive diversity, then extend to 1×4.
- For downlink, study fixed or explicitly oracle-selected rank-1 precoding and the resulting effective channel at srsUE RX0.
- For uplink, give each OCUDU RX port an independently modelled channel branch and let the gNB perform receive processing.
- Prove channel coefficients and port alignment with synthetic peers before relying on live srsUE behavior.

Technical requirements

#### Preserve the existing radio contract

- Keep srsUE at `nof_antennas = 1`; start OCUDU at `nof_antennas_dl: 2` and `nof_antennas_ul: 2`.
- Preserve the current 1×1 OCUDU↔srsUE attach and traffic path as a regression test.
- Keep precoding and beam weights in OCUDU or the RU; keep propagation, fading, delay, noise and superposition in this project.
- Align every gNB antenna stream to one logical sample/slot boundary and activate multi-port channel updates atomically.
- Require zero starvation events, sequence gaps, queue overflows and ZMQ errors in strict real-time validation.
- Measure CPU/CUDA correctness and fully label performance results by hardware, rate, topology, model, backend and duration.

Writing and claim requirements

#### Say exactly what is—and is not—proved

- Separate current implementation, approved design, unverified integration target and measured result.
- Describe the live topology as 2×1/4×1 DL MISO and 1×2/1×4 UL SIMO; never shorten it to end-to-end 4×4 MIMO.
- Do not call duplicated downlink samples “diversity” without verified precoding or a supported diversity mode.
- Do not claim UE receive beamforming, automatic PMI-driven beam control, rank\>1 SU-MIMO or same-PRB MU-MIMO.
- Keep unsupported and not-yet-demonstrated items visible in the hard-limits section.
- Update this controlling brief before expanding the report or implementation beyond rank-1 MISO/SIMO.

Sionna channel export

#### Export buffered channel state into the GPU IQ processor

- Run Sionna RT out of process as a channel-state producer; do not stream Sionna-generated IQ samples into OCUDU or srsUE.
- Use `Paths.cir()` as the canonical sparse, antenna-indexed channel representation and select one UE RX antenna for the DL row vector and one UE TX antenna for the UL column vector.
- Generate separate FDD downlink and uplink channel records at their actual carrier frequencies; bind every Sionna antenna index to an ordered OCUDU endpoint, element, pattern and polarization manifest.
- Export bounded geometry epochs and future complex-coefficient horizons with explicit port order, delays, validity, carrier/sample rate, first sample and coefficient period.
- Keep interpolation, delay history, convolution, superposition, receiver noise and deterministic ZMQ timing inside the C++/CUDA channel emulator.
- Implement record/replay before live export; live Sionna must remain buffered ahead of the broker’s absolute-sample consumption watermark.
- Start live attached-radio testing with static geometry plus Doppler. Do not claim seamless motion across scene retraces until geometry-transition continuity is implemented.
- Activate every coefficient in a directional vector atomically, define an explicit underrun policy and never substitute missing channel state with silent zero-filled IQ.
- Validate exported CIRs against `Paths.taps()`, record normalization/path-reduction metadata, and label Sionna integration as planned until these gates pass.

**Scope-change rule:** a new antenna count, UE, rank, beam-control method, channel-source contract or MU-MIMO objective is not an editorial detail. Record the user’s revised intent here first, then update the decision, architecture, roadmap, validation gates and limitations together. 
(mimo-integration-assessment-verdict)=
 01 · Direct answers

## Executive verdict

OCUDU can expose up to four downlink and four uplink antenna ports, while srsUE’s NR path is effectively one transmit stream, one receive stream and one layer. The approved target therefore keeps the UE rank-1 and adds diversity or array gain at the gNB side.

Can this project emulate MIMO now? No

It models one complex-IQ stream per logical device and one scalar TDL per directed edge. Multi-node fan-in is interference, not an antenna-channel matrix.

Does OCUDU support MIMO? Yes · up to 4×4

OCUDU’s current feature page states 4×4 downlink and uplink MIMO. Its history records DL up to four layers and later UL-MIMO work.

Does srsUE 5G support MIMO? Effectively rank-1

The LTE path supports 2-RX/TM1–4. The prototype NR path fixes default rank to one, decodes antenna 0 only, and lacks NR antenna-port demapping.

Does OCUDU CUDA implement a UE? No

OCUDU is a CU/DU/gNB RAN implementation. The CUDA fork accelerates gNB PHY, lower-PHY and fronthaul paths; it does not add a UE stack.

**Approved scope:** implement **2×1 downlink rank-1 MISO** and **1×2 uplink rank-1 SIMO** with one ZMQ endpoint per gNB antenna stream and one srsUE stream per direction. In parallel, export buffered Sionna RT CIR state into the GPU emulator through record/replay and then live look-ahead. Extend to 4×1 and 1×4 only after two-port timing and correctness pass. Do not include rank\>1 SU-MIMO or same-PRB MU-MIMO in this workstream. 
(mimo-integration-assessment-capabilities)=
 02 · System boundary

## Capability map

“MIMO support” means different things at each layer. The table separates antenna ports, spatial layers, propagation modelling, and UE capability.

| Component | Role | Antenna / layer capability | What it does not provide | Assessment |
|----|----|----|----|----|
| OCUDU baseline | 5G gNB: CU-CP, CU-UP, DU and RU integration | Official current claim: 4×4 DL and UL MIMO; configuration exposes `nof_antennas_dl` / `nof_antennas_ul` | No UE application; no channel emulator | MIMO gNB |
| CUDA-accelerated OCUDU fork | GPU backends inside the gNB | PUSCH 1–4 layers / 1–8 RX ports; PDSCH 4-layer benchmarks; multi-port OFH and SRS paths | No propagation matrix and no UE; large feature is not yet merged into OCUDU `dev` | Experimental fork |
| srsUE LTE | 4G UE | TM1–4 and two configured RX antennas; LTE spatial multiplexing is documented | This does not establish NR MIMO support | LTE 2×2 class |
| srsUE NR | Prototype 5G NSA/SA UE | Multiple RF buffers can exist, but the NR data path is effectively one port / one layer | No credible NR rank\>1 PDSCH or multi-layer UL validation target | NR rank-1 |
| ocudu-gpu-channel today | GPU channel + multi-node ZMQ broker | Many logical nodes and directed scalar links; receiver fan-in and TDL-A…E | No antenna-port dimension, channel matrix, spatial correlation, array response or beamforming | Scalar/SISO per node |
| Approved MVP | Rank-1 antenna-domain broker plus buffered Sionna channel-state input | DL 2 gNB TX × 1 UE RX; UL 1 UE TX × 2 gNB RX; expandable to 4×1 / 1×4; record/replay before live CIR export | No rank\>1 UE processing, UE-side receive beamforming, closed-loop PMI beam control, same-PRB MU-MIMO or synchronous ray tracing in the IQ path | Build next |

<span class="small">Primary evidence: [OCUDU current features](https://docs.ocudu.org/releases/), [srsRAN 4G feature list](https://docs.srsran.com/projects/4g/en/latest/feature_list.html), and the [srsUE NR PDSCH source](https://raw.githubusercontent.com/srsran/srsRAN_4G/release_25_10/lib/src/phy/phch/pdsch_nr.c).</span>

(mimo-integration-assessment-cuda)=
 03 · GitLab review

## What the CUDA-accelerated OCUDU fork supports

The fork is a gNB compute-acceleration project. It is useful to this work because it proves multi-layer GPU PHY paths and multi-port Split-8/7.2 handling—not because it already contains a MIMO propagation channel.

Upper PHY

#### PUSCH, PDSCH, SRS, PRACH

Resident PUSCH demodulation/equalization/LDPC integration, CUDA PDSCH block processing and direct-grid encoding, SRS estimation, and PRACH detection.

Lower PHY

#### Split-8 baseband

CUDA TX baseband processing, PUxCH RX OFDM demodulation, and PRACH OFDM demodulation. This sits adjacent to this project’s ZMQ IQ boundary.

Fronthaul

#### Split-7.2 Open Fronthaul

CUDA BFP/no-compression TX and RX, batched symbol modes, and device-visible grids that reduce intermediate host copies.

### MIMO-relevant support envelope

| Path | Supported / measured | Important boundary |
|----|----|----|
| Resident PUSCH | 1, 2, 3 or 4 layers; 1, 2, 4 or 8 RX ports | Normal CP Type-1 DM-RS product path; Type-2 mainly benchmark coverage. Transform-precoded PUSCH is one layer. |
| PDSCH direct device grid | One layer, plus identity-style allocations where layers = ports; benchmark demonstrates 4 layers / 4 ports | Other precoding layouts use the host resource-grid writer. |
| Open Fronthaul | 1–4 port benchmark matrix for BFP compression/decompression | This accelerates IQ representation and movement, not the over-the-air channel. |
| GPU selection | `nvcuda_device_id` selects a GPU for gNB/DU applications | Selection is not the same as distributing one workload across several GPUs. |

### Published quick measurements

21.2×PUSCH · 4 layers / 8 RX ports · 100 MHz 3.36×PDSCH p50 · 4 layers / 4 ports · 100 MHz 72.58×OFH RX BFP p50 · 4 ports · 100 MHz −0.1 dBPUSCH sensitivity delta at 10% BLER

<span class="small">These are fork-author measurements on DGX Spark / GB10, not measurements from this project. The same report shows CUDA losing on small 20 MHz single-layer workloads, which is why acceleration defaults to `auto`. See the [fork’s acceleration guide](https://gitlab.com/ocudu/work_groups/wg1_hw_accel/cuda_accelerated_ocudu/-/raw/nvcuda_accel_02/docs/phy_cuda_acceleration.md) and [OCUDU MR !1180](https://gitlab.com/ocudu/ocudu/-/merge_requests/1180).</span>

**Upstream status:** the full CUDA contribution is still an open, large merge request. OCUDU requested a six-MR split; as of this snapshot, [!1238](https://gitlab.com/ocudu/ocudu/-/merge_requests/1238) contributes build/ADT plumbing only and [!1262](https://gitlab.com/ocudu/ocudu/-/merge_requests/1262) contributes a PRACH detector without application-level selection. Pin the fork commit for experiments and do not treat CUDA support as part of baseline OCUDU `dev` yet. 
(mimo-integration-assessment-ue)=
 04 · UE reality check

## srsUE: LTE MIMO is not 5G NR MIMO

The documentation can look more capable than the NR implementation because the MIMO section describes LTE. Direct inspection of the latest srsRAN_4G release resolves the ambiguity.

srsUE LTE path TM1 / TM2 / TM3 / TM4

- `nof_rx_ant = 2` is documented.
- TM3/TM4 allow spatial multiplexing with a second receive chain.
- This is the mature 4G UE feature set.

≠ srsUE 5G NR path Rank 1 in practice

- Default 10/20 MHz carriers set `max_mimo_layers = 1`.
- PDSCH reads `sf_symbols[0]` / `ce[0][0]` and calls the single-stream predecoder.
- Antenna-port demapping is explicitly unimplemented.
- CSI measurement reports one port; NR UL is initialized with `tx_buffer[0]`.

**Engineering conclusion:** adding a second ZMQ channel or setting `nof_antennas = 2` does not turn srsUE’s NR PHY into a 2×2 MIMO UE. It may open buffers at the RF layer, but the NR PDSCH/CSI/UL processing path does not provide rank\>1 behavior to validate the channel matrix.

### What rank 1 still permits with OCUDU

Downlink

#### 2×1 or 4×1 MISO

OCUDU transmits one PDSCH layer through two or four gNB ports; the channel produces one effective stream for srsUE RX0. Fixed or externally selected precoding can provide beam or array gain, but srsUE does not provide a credible multi-port PMI feedback loop.

Uplink

#### 1×2 or 1×4 SIMO

srsUE transmits one PUSCH layer from TX0; the channel produces independently faded copies at two or four OCUDU receive ports. OCUDU performs the receive combining, so this is the strongest end-to-end diversity target.

Explicit boundary

#### One UE layer throughout

The UE cannot steer a receive beam with one physical antenna. The scope also excludes rank-2 decoding, standardized transmit-diversity claims without a supported gNB/UE mode, and two UEs sharing PRBs through MU-MIMO.

**Endpoint contract:** keep srsUE at `nof_antennas = 1`. Start OCUDU at `nof_antennas_dl: 2` and `nof_antennas_ul: 2`; after validation, raise either gNB-side value to four. The port count changes diversity/array opportunity, not the one-layer UE rank.

The official OCUDU tutorial independently calls srsUE’s 5G support a proof-of-concept, says it is no longer actively developed, configures `nof_antennas = 1`, and states that OCUDU itself does not include a UE. See the [OCUDU srsUE tutorial](https://gitlab.com/ocudu/ocudu_docs/-/raw/main/docs/tutorials/srsue/index.md).

(mimo-integration-assessment-limits)=
 05 · Hard limits

## What this setup cannot do

The approved topology is useful precisely because its claims are narrow. Some items are impossible with one physical UE antenna, some are absent from srsUE’s NR PHY, and others remain unimplemented or unverified integration work.

| Capability | Status | Why it is unavailable | What would be required |
|----|----|----|----|
| Multi-port operation in this broker today | Not implemented | The current local data model has one TX/RX endpoint pair and one scalar channel per directed edge. | Port arrays, grouped ZMQ timing, directional channel vectors, CPU/CUDA processing and new validation tests. |
| Live Sionna→GPU channel updates today | Not implemented | The report and bridge plan define the contract, but the exporter, bounded binary ingress, look-ahead buffer and sample-indexed activation path do not yet exist. | Build and validate static export, record/replay, buffered coefficient horizons, telemetry and the explicit underrun policy before enabling live mode. |
| Arbitrary raw Sionna path sets | Cannot ingest directly | The current CUDA state accepts at most 32 taps per directional coefficient and a 128-sample delay-history ring; a scene can exceed both. | Deterministic per-antenna-pair clustering/pruning, retained-energy reporting and rejection below a configured fidelity threshold—or a larger/dynamic device representation. |
| Seamless retraced Sionna mobility | Not implemented | Retracing can change delays and path identity. Current geometry activation resets affected delay history and exposes a warmup interval. | A validated fixed-delay-grid or dual-state geometry transition, plus aggregate-channel continuity gates across retrace boundaries. |
| Live 2×1 or 4×1 OCUDU→srsUE proof today | Not yet demonstrated | The official OCUDU reference uses one gNB/UE channel, and this project has not yet tested a fixed rank-1 multi-port PDSCH through srsUE. | The two-port transport spike, a verified OCUDU/RU rank-1 precoder and successful srsUE attach/traffic are explicit delivery gates. |
| Rank-2 or higher downlink SU-MIMO | Cannot with srsUE NR | srsUE’s NR defaults and PDSCH path are effectively one layer and do not implement antenna-port demapping. | A source-proven multi-layer NR UE such as a validated OAI nrUE configuration, a UE simulator or a COTS UE. |
| Multi-layer uplink from srsUE | Cannot with srsUE NR | The NR uplink worker initializes and uses TX buffer zero; adding RF buffers does not create additional PUSCH layers. | A UE with multi-port PUSCH generation and rank/layer support. |
| UE receive diversity or receive-beam steering | Cannot with one physical RX | One antenna produces one scalar observation, so the UE has no second spatial sample to combine or phase-steer. | At least two physical receive elements and a UE/RF path that exposes or controls them; an analog array hidden behind one RF chain would need separate hardware control. |
| Automatic closed-loop DL beam control | Not credible with srsUE | srsUE’s CSI measurement path reports one port and does not provide the multi-port PMI feedback needed to select weights from the measured spatial channel. | A capable CSI-RS/PMI UE or an explicitly labelled fixed/oracle weight selected outside srsUE. |
| Transmit diversity by duplicating one waveform | Cannot be claimed | Uncontrolled copies may add or cancel. True gain requires channel-aware phase weights or a source-backed transmit-diversity code supported at both ends. | Verified precoder metadata and channel-aware beam weights, or a standards-supported diversity mode demonstrated with the selected gNB and UE. |
| Two rank-1 srsUEs on the same PRBs via MU-MIMO | Not with stock path | It requires same-PRB spatial scheduling, user grouping, distinct reference signals, CSI and multi-user precoding; those are outside this project and not supplied by the current OCUDU+srsUE path. | OCUDU MAC/PHY changes plus a validated CSI or oracle-channel strategy. This would explicitly expand the project beyond channel emulation. |
| End-to-end 4×4 MIMO with srsUE | Cannot | Four OCUDU DL/UL ports do not create four UE antennas or four UE layers. The actual srsUE topology remains 4×1 DL and 1×4 UL. | A four-port/layer-capable UE and corresponding RU or UE simulator. |
| Unrestricted 5G SA scenarios | Cannot with this PoC UE | The official OCUDU tutorial limits srsUE SA to 15 kHz SCS, FDD, 5/10/15/20 MHz and no handover. | A more complete UE for TDD, wider bandwidths, other SCS values or handover validation. |

(mimo-integration-assessment-project)=
 06 · Local code assessment

## What this project has—and what multi-port rank 1 adds

The current implementation already has the hard real-time broker, per-edge channel chains, GPU superposition, TDL fading and live OCUDU↔srsUE validation. Its missing dimension is antenna port.

**OCUDU gNB**<span class="small">1 scalar TX IQ\
1 scalar RX IQ</span> → **Current GPU broker**<span class="small">Directed graph of scalar links</span>TDLCFOphaseAWGN → **srsUE NR**<span class="small">1 effective NR layer\
1 effective port</span>

Current live path: multi-node and multi-link, but one complex stream per node. Receiver fan-in sums users/cells; it does not form H<sub>r,t</sub>.

Evidence in local types

- [`IqSample`](../../../include/ocudu_gpu_channel/iq.h) is one scalar complex sample.
- [`DeviceConfig`](../../../include/ocudu_gpu_channel/config.h) has one TX/RX endpoint pair, not endpoint arrays or port counts.
- [`SuperpositionInput`](../../../include/ocudu_gpu_channel/processing.h) identifies a link/model and sample span, not a TX antenna.
- TDL taps have delay, gain, phase and optional LOS—but no RX/TX port index, angle, polarization or array geometry.

Useful OCUDU transport fact

OCUDU’s Split-8 ZMQ translator already extracts multiple `tx_port*` and `rx_port*` entries, maps one address to every configured antenna, and constructs multi-channel ZMQ streams.

This means the first compatible wire format can remain **one REQ/REP endpoint pair per antenna channel**. The broker—not OCUDU—needs the larger port-bundle model.

[Inspect the pinned OCUDU translator](https://gitlab.com/ocudu/work_groups/wg1_hw_accel/cuda_accelerated_ocudu/-/blob/b708b13730a1b6181d930d594196a2a9b54933a0/apps/units/flexible_o_du/split_8/helpers/ru_sdr_config_translator.cpp#L111)

(mimo-integration-assessment-design)=
 07 · Proposed architecture

## Rank-1 directional channels first

The approved channel is rectangular in each direction. Downlink is a 1×N<sub>gNB-TX</sub> row vector that combines gNB ports into one srsUE receive stream; uplink is an N<sub>gNB-RX</sub>×1 column vector that produces one independently faded stream per gNB receiver.

DL: y<sub>UE</sub>\[n\] = Σ<sub>t</sub> Σ<sub>k</sub> h<sup>DL</sup><sub>t,k</sub>\[n\] · x<sub>gNB,t</sub>\[n − d<sub>t,k</sub>\] + w<sub>UE</sub>\[n\]\
UL: y<sub>gNB,r</sub>\[n\] = Σ<sub>k</sub> h<sup>UL</sup><sub>r,k</sub>\[n\] · x<sub>UE</sub>\[n − d<sub>r,k</sub>\] + w<sub>gNB,r</sub>\[n\] **OCUDU gNB**<span class="small">DL TX0 / TX1 \[up to TX3\]\
UL RX0 / RX1 \[up to RX3\]</span> ⇄ **Rank-1 GPU channel**<span class="small">Atomic gNB port bundle per slot</span>h<sup>DL</sup><sub>0</sub>(τ,t)h<sup>DL</sup><sub>1</sub>(τ,t)h<sup>UL</sup><sub>0</sub>(τ,t)h<sup>UL</sup><sub>1</sub>(τ,t) ⇄ **srsUE 5G**<span class="small">DL RX0 / UL TX0\
one effective NR layer</span>

Approved target boundary. The two-port MVP is DL 2×1 MISO plus UL 1×2 SIMO; the four-port extension is 4×1 plus 1×4. The UE remains rank-1 throughout.

### Required data-model and runtime changes

| Area | Current | Rank-1 MVP | Why |
|----|----|----|----|
| Device identity | Logical `device_id` | `RadioPortKey { device_id, port }` | Ports must be independently addressable without pretending each antenna is a separate UE. |
| Endpoints | One TX and one RX endpoint | `tx_endpoints[]` / `rx_endpoints[]` | Matches OCUDU’s indexed, per-channel ZMQ model. |
| Link model | One scalar channel chain | One DL row vector and one UL column vector of tap sets | Represents every gNB-side propagation branch while fixing the UE-side dimension to one. |
| Processor input | `[source][sample]` | DL `[gNB_tx_port][sample]`; UL `[UE_tx0][sample]` | Downlink accumulates gNB transmit ports; uplink reads one UE stream. |
| Processor output | `[destination][sample]` | DL `[UE_rx0][sample]`; UL `[gNB_rx_port][sample]` | Downlink produces one effective UE channel; uplink preserves independent receive branches. |
| Timing | Per-device scalar request/reply flow | Grouped slot barrier across all configured gNB ports | Independent gNB-port drift destroys coherent DL combination and UL diversity. |
| Noise | Scalar receiver model | One DL noise process at UE RX0 and one UL noise process per gNB RX port | Noise is receiver-side, not independently added for every propagation coefficient. |
| Control plane | Per-link scalar/profile updates | Atomic directional-vector version and activation sample | A partially updated port vector creates an unintended intermediate beam or diversity channel. |

### GPU kernel shape

Launch index

#### (direction, destination RX port, sample)

The DL destination count is one and accumulates gNB TX ports × taps. Each UL output owns one gNB receive-port sample and accumulates its tap set.

Memory layout

#### Port-major contiguous slots

Pack DL as `[gNB_tx_port][sample]` and UL as `[gNB_rx_port][sample]`; coalesce samples and retain exact 1×1 compatibility.

Reference contract

#### CPU ↔ CUDA parity

Use the CPU path as the oracle for branch isolation, coherent DL sums, UL receive vectors, delay history, fading continuity and runtime updates.

**Do not call sample duplication “diversity.”** A fixed DL weight vector provides guaranteed array gain only when its phases match the channel; closed-loop weight selection is outside srsUE’s credible CSI capability. First prove deterministic MISO/SIMO coefficient vectors, synchronization and ZMQ transport. Add spatial correlation or CDL only when a source-backed beam/diversity experiment requires it. 
(mimo-integration-assessment-sionna)=
 08 · External channel source

## Export Sionna channel state—not IQ samples

Sionna RT should sit beside the broker as a Python channel-state producer. It computes the site-specific, antenna-indexed CIR; the existing C++/CUDA path remains responsible for deterministic IQ timing, delay history, convolution, superposition and ZMQ radio behavior.

**Sionna RT 2.0.1**<span class="small">Scene + arrays + mobility\
`PathSolver → Paths.cir()`</span> → **Sidecar + look-ahead buffer**<span class="small">Validate, bound, record/replay\
index by absolute IQ sample</span> → **CUDA MISO/SIMO TDL**<span class="small">Preloaded coefficient horizon\
never waits for ray tracing</span>

Sionna produces channel coefficients out of process. The broker consumes only validated, already-buffered state, so a slow retrace cannot block an OCUDU REQ/REP IQ transaction.

**Recommended contract:** use Sionna `Paths.cir()` as the canonical sparse interchange. Its coefficient tensor is `[RX, RX antenna, TX, TX antenna, path, time]`, while delays omit the time axis. Configure a one-antenna UE array and preserve the gNB antenna axis to form the approved DL row and UL column. Use `Paths.taps()` as an independent offline oracle with declared bandwidth, sampling rate, lag range and normalization—not as a promise of bit-exact fractional-delay samples.

### Exact mapping for this FDD rank-1 target

| Direction | Sionna roles and carrier | Exported coefficient layout | GPU IQ equation |
|----|----|----|----|
| Downlink 2×1, later 4×1 | OCUDU is the Sionna transmitter with 2/4 gNB antenna indices; srsUE is a one-antenna receiver. Set the scene to the actual DL carrier. | `[time][1 UE RX][N gNB TX][tap]` | `y_UE[n] = Σ_tx Σ_k h_DL[tx,k,n] · x_tx[n−τ_tx,k]` |
| Uplink 1×2, later 1×4 | srsUE is a one-antenna Sionna transmitter; OCUDU is the 2/4-antenna receiver. Set the scene to the actual UL carrier. | `[time][N gNB RX][1 UE TX][tap]` | `y_rx[n] = Σ_k h_UL[rx,k,n] · x_UE[n−τ_rx,k]` |

**Do not obtain FDD uplink by merely reversing the downlink tensor.** Sionna’s `reverse_direction` changes tensor roles; it does not change a downlink-frequency ray trace into an uplink-frequency channel. Build separate directional records at the configured DL and UL carriers. Any reciprocity shortcut must be a separately measured and labelled approximation. Geometry epoch · infrequent

#### Path layout and delays

A retrace publishes port order, path-validity mask, fractional delays, carrier/sample rates, scene hash and an activation sample. Because the tap layout changed, this event may rebuild polyphase state and invoke the existing warmup contract.

Coefficient horizon · continuous

#### Complex a<sub>k</sub>(t) grid

Future complex path weights arrive in chunks and activate by absolute IQ sample index. They update weights only: the IQ delay line is preserved and the CUDA kernel interpolates between coefficient-grid points.

### Producer workflow and canonical extraction

Sionna sidecar

#### Trace, extract, validate, reduce, serialize

1.  Set direction-specific radio roles, carrier, array geometry, patterns, polarization, positions, orientations and velocities.
2.  Run `PathSolver` with recorded mechanisms, seed and `synthetic_array` choice.
3.  Extract a future complex grid with physical delays and Numpy output.
4.  Apply `Paths.valid`, reject non-finite data, map antenna indices to named radio ports and reduce paths per antenna pair.
5.  Write one geometry epoch plus sequenced coefficient chunks to a durable trace before enabling live streaming.

Reference call

#### Make every default explicit

    a, tau = paths.cir(
        sampling_frequency=iq_rate_hz / period_samples,
        num_time_steps=num_grid_points,
        normalize_delays=False,
        out_type="numpy",
    )
    valid = paths.valid.numpy()

`period_samples` must be a positive integer. Grid point `i` is bound to broker sample `first_sample + i × period_samples`; wall-clock time is diagnostic only.

**Coefficient ownership:** `Paths.cir()` returns the complex baseband path coefficient, including carrier-delay phase and Doppler evolution. When this source is active, do not add the carrier phase again or quietly cascade the same tap through a second internal path-loss/Jakes model. OCUDU/RU owns transmit precoding; Sionna owns the propagation coefficient; the emulator owns sampled delay, interpolation, convolution, superposition and receiver noise.

### Why the current control message is not enough

| Surface | Use it for | Do not use it for | Required addition |
|----|----|----|----|
| JSON `profile_swap` | One static Sionna snapshot whose delays fit state prepared from startup YAML | Per-slot path coefficients or a longer geometry: activation resets history and warmup, while the current device state is capped at 32 taps and a 128-sample ring | Bound-checked geometry activation plus coefficient-only updates that preserve delays and history |
| Multi-link control batch | Targeting one numerical per-link slot across several updates | Providing a global sample-boundary barrier or transporting dense complex multi-port horizons through sequential JSON REQ/REP messages | Directional port-bundle barrier plus versioned binary channel frames on a dedicated bounded endpoint |
| Per-link slot index | Operator-facing control timing and existing scalar updates | Authoritative Sionna timing when broker serves can be variable-sized | `first_sample` plus `coefficient_period_samples` on every chunk |
| Current processor API | Scalar directed edges and summed fan-in to one logical destination | Preserving and atomically switching a DL row or UL column of antenna-indexed coefficients | Port-aware geometry and active/next coefficient-horizon buffers selected at one destination sample cursor |
| Telemetry PUB | Accepted watermark, buffered lead, active epoch and failure counters | Carrying the channel itself | Producer paces look-ahead from `buffered_until_sample` |

### Extraction rules that must be explicit

Timing

#### Preserve physical delay

Call `cir(normalize_delays=False)` when time of flight matters, then convert seconds to fractional IQ samples. Relative-delay mode is allowed only as named metadata.

Amplitude

#### Keep linear path gain

`cir()` has no energy-normalization switch in Sionna RT 2.0.1. Preserve its linear complex gain, mask invalid paths and reject NaN/Inf values before the frame reaches the broker.

Bounds

#### Cluster and report

Within each RX/TX antenna pair, cluster nearby delays, sum complex voltage, retain at most 32 taps whose maximum delay fits the 128-sample device ring, and report retained-energy ratio. Never silently truncate a rich scene.

### Array fidelity and stable port identity

Port manifest

#### Names—not array positions by accident

Bind every Sionna antenna index to a direction, OCUDU ZMQ endpoint/port, element position, orientation, polarization and antenna pattern. Include this ordered manifest in the topology hash.

Synthetic array · default

#### Fast plane-wave approximation

Sionna traces between array centers and applies element phase shifts synthetically. Use it when array aperture is small relative to device/scatterer distances, and record the approximation in every epoch.

Explicit array

#### Trace every antenna pair

Use `synthetic_array=False` when element-specific blockage, near-field behavior or aperture delay differences matter. Benchmark it separately; never mix synthetic and explicit epochs in one trace.

**Polarization is part of the port map.** In Sionna, a dual-polarized physical element contributes two linearly polarized antenna indices. Four Sionna antenna indices therefore do not automatically mean four physical element locations, and four OCUDU ports must not be mapped by count alone.

### Buffering, activation and failure state

| State | Broker rule | Failure behavior |
|----|----|----|
| Epoch received | Validate run id, direction, carrier, sample rate, topology hash, port order, dimensions, delays, validity and CRC. Keep pending. | Reject malformed, stale, out-of-window or topology-mismatched frames before GPU upload. |
| Horizon buffered | Require a coefficient chunk covering `valid_from_sample`; repeat exactly one endpoint between adjacent chunks and verify it within numeric tolerance. | Reject gaps, late chunks, conflicting overlaps and mismatched repeated endpoints. |
| GPU resident | Upload the full future horizon asynchronously and mark it ready with a CUDA event before its deadline. | The IQ serve path never waits for Python, ray tracing, allocation, parsing or an unfinished H2D copy. |
| Active | Atomically select the whole DL row or UL column at one absolute destination-sample boundary; interpolate coefficient grid points without resetting delay history. | Individual antenna coefficients cannot change epoch independently. |
| Underrun | Increment counters, mark telemetry degraded and apply only the configured grace/fallback. | `fail`, bounded `hold_last`, static profile or internal Jakes; never silent zero-fill. |

coefficient_rate = IQ_rate / period_samples\
lookahead_samples ≥ ceil(IQ_rate × (p99 trace + extract + serialize + transport + H2D + safety margin))

The coefficient period is selected by sweeping the maximum `Paths.doppler` and measuring phase/interpolation error. Look-ahead is selected from measured tail latency, not a convenient fixed number. Buffer memory for one complex-f32 horizon is approximately `8 × time_points × RX ports × TX ports × taps` bytes, before metadata and double buffering.

### How to use `Paths.taps()` correctly

Independent reference

#### Lock every numerical choice

Set `bandwidth`, `sampling_frequency`, `l_min`, `l_max`, `num_time_steps`, `normalize=False` and the same delay-normalization mode. Compare impulse response, energy, peak delay and frequency response.

Known model difference

#### Ideal sinc versus bounded filter

Sionna’s discrete taps use an ideal sinc over the requested lag range; the project uses an 8-tap Hamming-windowed sinc for fractional delay. Integer-delay fixtures can match closely. Fractional-delay gates need declared tolerances plus the project’s bounded-filter reference.

**Live means buffered ahead, not ray-traced synchronously.** Sionna documents Doppler evolution as a fast short-horizon approximation and moving/retracing geometry as the more accurate longer-horizon method. Start attached-radio work with static geometry plus Doppler. A retrace can change delays and path identities; today that implies a geometry activation, history reset and warmup. Seamless long motion needs a later explicit transition—such as overlapping old/new FIR state or a fixed discrete-delay grid—and is not yet guaranteed. The producer still needs record, replay and live modes plus an explicit underrun policy. It must never zero-fill a missed channel frame.

Full message shapes, continuity rules, failure policy, telemetry and validation gates are specified in [the Sionna live-channel bridge plan](../../development/designs/sionna-live-channel.md).

(mimo-integration-assessment-roadmap)=
 09 · Delivery plan

## Phased integration roadmap

The dependency path deliberately proves two gNB ports before four. The scalar Sionna exporter and record/replay format can proceed in parallel, but live multi-port CIR ingestion merges only after deterministic 2×1/1×2 channel vectors pass.

#### Port-bundle transport spike

Pin a known OCUDU baseline. Configure two gNB DL TX addresses and two gNB UL RX addresses while retaining one srsUE endpoint in each direction. Use synthetic peers first to prove slot alignment, teardown and backpressure.

Exit: every configured gNB port remains aligned with the one-port UE side and all data-integrity counters remain zero.

#### Deterministic 2×1 / 1×2 channel

Add port-aware configuration, a CPU reference and CUDA processing for a DL row vector and UL column vector. Start with fixed complex gains and directional TDL taps; retain exact 1×1 backward compatibility.

Exit: branch isolation, coherent/cancelling DL sums, independent UL branches and delayed/faded vectors match the CPU oracle.

#### Merge the Sionna record/replay bridge

Bring in sparse antenna-indexed CIR epochs, a durable trace and buffered coefficient horizons indexed by absolute IQ sample. Extract separate FDD DL rows and UL columns with hashed port manifests; add live path solving only after replay is deterministic.

Exit: integer-delay and bounded fractional-delay oracle gates pass; all directional coefficients activate together with no coefficient-update delay-line reset or underrun.

#### Live OCUDU + srsUE integration

Run srsUE with one antenna and OCUDU with two DL and two UL ports. Begin with static Sionna geometry plus Doppler, then verify rank-1 PDSCH/PUSCH attach, PDU session and traffic through controlled 2×1 DL and 1×2 UL channels. Keep precoding in OCUDU/RU; an emulator-supplied weight is labelled an oracle experiment.

Exit: stable attach and traffic with the expected per-branch power/SNR behavior, measured look-ahead and zero channel underrun, broker overflow, gap or ZMQ errors.

#### Extend the gNB side to four ports

Raise OCUDU to four DL TX and four UL RX ports without changing the one-port srsUE contract. Reuse the directional-vector implementation and repeat correctness, timing and real-time measurements.

Exit: validated 4×1 DL and 1×4 UL behavior; the report continues to claim one layer, not 4×4 UE spatial multiplexing.

#### Spatial channel fidelity

If fixed-beam or diversity research requires it, add spatial correlation or CDL with array geometry, angles and polarization. Activate vector updates through the port-bundle sample barrier rather than independent per-link snapshots.

Exit: statistical validation against a trusted TR 38.901 reference plus explicit precoder/beam metadata. 
(mimo-integration-assessment-validation)=
 10 · Evidence gates

## Definition of done

Channel correctness

- 1×1 current configurations remain behaviorally compatible.
- DL antenna-0-only and antenna-1-only cases reach UE RX0 with the expected gain and delay.
- A DL phase sweep produces the expected coherent addition and cancellation; no gain is attributed to duplication alone.
- UL RX0 and RX1 branches preserve independent gain, delay, fading and noise.
- Fractional delay and cross-slot history remain continuous across coefficient-only updates.
- Integer-delay Sionna fixtures match peak delay/phase; fractional-delay fixtures meet declared time, frequency and energy tolerances against `Paths.taps()` and the bounded-filter reference.
- Separate FDD DL/UL solves preserve carrier, direction, port order, polarization and array metadata.
- CPU and CUDA agree within the established numeric tolerance for 2×1/1×2 and later 4×1/1×4.

System behavior

- All configured gNB antenna ports use the same logical slot boundary.
- No gNB port gets ahead when another port stalls.
- Directional-vector updates activate at one declared absolute sample boundary; adjacent chunks repeat and verify one endpoint.
- srsUE remains configured for one RX/TX stream and establishes an SA PDU session through the two-port gNB setup.
- Sionna stays ahead of the consumption watermark with zero rejected, late or underrun frames in the strict run.
- Broker gap/overflow/ZMQ-error counters stay zero during the test.
- 2-port and 4-port real-time latency and Sionna tail latency are measured on the target GPU arrangement with complete workload labels.
- No result is described as rank\>1, UE receive beamforming, seamless retraced mobility or MU-MIMO.

### Key risks and controls

| Risk | Impact | Control |
|----|----|----|
| srsUE NR ceiling | Port count could be mistaken for multi-layer MIMO | Keep srsUE at one RX/TX stream and label every result rank-1 MISO or SIMO. |
| DL feedback limitation | srsUE’s one-port CSI cannot drive credible closed-loop PMI beam selection | Start with fixed or oracle-labelled precoding; keep the precoder in OCUDU/RU and report how weights were selected. |
| Port synchronization | Unintended DL phase rotation, loss of coherent gain and OFDM corruption | Atomic port bundles, grouped barriers, common sequence/slot metadata and explicit skew counters. |
| Scaling | Directional work grows roughly with (N<sub>DL-TX</sub> + N<sub>UL-RX</sub>) × taps | Ship two gNB ports first, measure H2D/kernel/D2H separately, then gate four ports. |
| Wrong spatial statistics | Independent fading coefficients can create unrealistic arrays | Label the MVP configurable directional TDL; add correlation/CDL before spatial-research claims. |
| Wrong port or polarization map | A plausible tensor drives the wrong OCUDU RF streams | Hash the ordered direction/carrier/endpoint/antenna/polarization manifest and test one branch at a time. |
| Synthetic-array approximation | Plane-wave phase shifts hide element-specific blockage, near-field or aperture-delay effects | Record `synthetic_array`, compare against explicit tracing for the chosen scene, and use explicit arrays where those effects are claimed. |
| FDD reciprocity shortcut | UL is evaluated with DL-frequency coefficients or wrong radio roles | Create separate DL and UL epochs at their actual carriers; label any reciprocity approximation explicitly. |
| Shared GPU contention | gNB PHY and channel emulator miss deadlines | Profile colocated workloads; consider separate devices/process affinity before optimizing kernels. |
| Sionna producer misses its horizon | Channel state arrives after the corresponding IQ samples | Keep Sionna out of the serve path, buffer by absolute sample index, expose lead/late/underrun counters, and configure an explicit fallback. |
| Retrace during attached traffic | New delays/path identities reset history and expose warmup artefacts | Start with static geometry plus Doppler; treat retrace as discontinuous until a fixed-grid or dual-state geometry transition is implemented. |
| Double-counted propagation | Path loss, carrier phase or Doppler is applied twice | Declare coefficient ownership; disable overlapping internal path-loss/fading for Sionna-controlled taps and keep receiver noise applied once. |
| Implicit normalization or truncation | Scene path loss/delay spread no longer matches the intended channel | Record delay and energy modes, retained-energy ratio, tap reduction, scene hash and solver seed with every epoch. |
| CUDA fork churn | Integration breaks as six MRs evolve | Pin `b708b13` for the spike, keep baseline OCUDU compatibility, and isolate fork-specific launch/config files. |

(mimo-integration-assessment-decision)=
 11 · Decision

## Recommended project decision

**Approve a rank-1 asymmetric multi-antenna MVP and a Sionna RT channel-state export path.** The live radio target is OCUDU 2×1 downlink MISO plus 1×2 uplink SIMO with one-port srsUE 5G; expand the gNB side to four ports only after the two-port gate. Sionna supplies buffered sparse CIR geometry and coefficient horizons, while the GPU emulator owns timed IQ processing. Keep precoding in the gNB/RU, propagation in this project, and rank\>1 SU-MIMO and same-PRB MU-MIMO deferred. Use srsUE for

#### Live rank-1 proof

1×1 regression, 2×1/4×1 DL effective-channel reception, 1×2/1×4 UL diversity, SA attach, PDU session and traffic.

Use synthetic peers for

#### Coefficient proof

Branch isolation, DL phase/weight sweeps, UL independent branches, cross-port timing, delay/fading continuity and performance.

Use Sionna RT for

#### Channel-state export

Recorded and live buffered CIR geometry, delays and complex coefficient horizons; the GPU emulator applies them to real-time IQ.

Do not claim

#### Rank greater than one

No multi-layer UE decode, UE receive beam steering, automatic PMI-driven beam control or two-user same-PRB MU-MIMO is part of this decision.

(mimo-integration-assessment-sources)=
 12 · Source register

## Primary evidence

Claims above are based on first-party documentation and source code. Fork claims are explicitly labelled because they are not yet baseline OCUDU features.

[OCUDU current releases and feature list](https://docs.ocudu.org/releases/)

Official statement of 4×4 MIMO DL/UL and current release context.

[OCUDU 4T4R test-mode example](https://docs.ocudu.org/tutorials/accx00/)

Official configuration evidence for `nof_antennas_dl: 4`, `nof_antennas_ul: 4` and a reported 4T4R cell.

[OCUDU release notes](https://docs.ocudu.org/releases/release_notes/)

DL MIMO up to four layers, single-layer closed-loop UL-MIMO history, and later UL-MIMO release entry.

[OCUDU repository README](https://gitlab.com/ocudu/ocudu/-/raw/dev/README.md)

Defines OCUDU as a 5G CU/DU RAN with L1/2/3, rather than a UE implementation.

[Official OCUDU + srsUE tutorial](https://gitlab.com/ocudu/ocudu_docs/-/raw/main/docs/tutorials/srsue/index.md)

States that OCUDU does not include a UE, labels srsUE as a prototype/PoC, configures one antenna, and records the SA limits: 15 kHz SCS, FDD, 5/10/15/20 MHz and no handover.

[CUDA feature MR !1180](https://gitlab.com/ocudu/ocudu/-/merge_requests/1180)

Full CUDA contribution scope, MIMO measurements, limitations, test coverage and split plan.

[CUDA acceleration guide on `nvcuda_accel_02`](https://gitlab.com/ocudu/work_groups/wg1_hw_accel/cuda_accelerated_ocudu/-/raw/nvcuda_accel_02/docs/phy_cuda_acceleration.md)

Operator selectors, PUSCH/PDSCH layer/port bounds, multi-GPU device selection and benchmark details.

[srsRAN 4G features](https://docs.srsran.com/projects/4g/en/latest/feature_list.html) and [srsUE advanced usage](https://docs.srsran.com/projects/4g/en/next/usermanuals/source/srsue/source/4_ue_advanced.html)

Separates the mature LTE MIMO feature from prototype 5G support.

[srsUE NR default PHY config](https://raw.githubusercontent.com/srsran/srsRAN_4G/release_25_10/lib/src/common/phy_cfg_nr_default.cc)

10/20 MHz reference carriers set `max_mimo_layers = 1`.

[srsUE NR PDSCH implementation](https://raw.githubusercontent.com/srsran/srsRAN_4G/release_25_10/lib/src/phy/phch/pdsch_nr.c)

Uses antenna 0 / channel estimate 0, calls the single-stream predecoder, and marks antenna-port demapping unimplemented.

[srsUE NR component-carrier worker](https://raw.githubusercontent.com/srsran/srsRAN_4G/release_25_10/srsue/src/phy/nr/cc_worker.cc) and [NR state/CSI path](https://raw.githubusercontent.com/srsran/srsRAN_4G/release_25_10/srsue/hdr/phy/nr/state.h)

Multiple RX buffers exist, but UL uses TX buffer zero and CSI channel measurements support one port.

[Local channel configuration types](../../../include/ocudu_gpu_channel/config.h), [processing API](../../../include/ocudu_gpu_channel/processing.h), [CUDA channel bounds](../../../include/ocudu_gpu_channel/device_channel.h), and [project README](../../../README.md)

Evidence for today’s scalar per-node endpoints and superposition API, plus the current 32-tap and 128-sample device limits.

[Sionna RT 2.0.1 `Paths` API](https://nvlabs.github.io/sionna/rt/api/paths.html)

Defines the antenna-indexed CIR, discrete taps, delay normalization, time sampling, Doppler evolution, validity mask and Numpy export surface used by the proposed bridge.

[Sionna RT mobility tutorial](https://nvlabs.github.io/sionna/rt/tutorials/Mobility.html)

Separates short-horizon Doppler evolution from moving and retracing geometry, and states where the fixed-path approximation loses accuracy.

[Sionna RT 2.0.1 path-solver API](https://nvlabs.github.io/sionna/rt/api/paths_solvers.html) and [path-solver technical report](https://nvlabs.github.io/sionna/rt/tech-report/S3.html)

Establish the default synthetic-array behavior, the explicit per-element alternative, and the plane-wave approximation’s array-aperture/distance validity boundary.

[Sionna RT repository](https://github.com/NVlabs/sionna-rt)

Standalone-package boundary, current 2.0.1 release context and Apache-2.0 licensing.

[Local Sionna live-channel bridge plan](../../development/designs/sionna-live-channel.md)

Detailed wire contract, continuity rules, bounded path reduction, buffering, failure behavior, phased delivery and validation matrix.

**Assessment boundary.** No rank-1 multi-port or Sionna channel-processor changes are represented as already complete here; this document records the approved integration decision and implementation blueprint. External projects can change after the research snapshot, especially the open CUDA merge requests and Sionna APIs.

Generated from docs/mimo-integration-report.html
