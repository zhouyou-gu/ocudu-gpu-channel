# CMX500–X310–GPU–srsUE feasibility study

Status: **conditionally feasible; hardware validation required**
Date: **2026-08-18**
Recommended first target: **5G SA, FR1 band n3, FDD, 20 MHz, 15 kHz SCS, SISO**

## Executive decision

The proposed connection is feasible if the X310 is used as the actual RF front end of srsUE and the GPU channel is inserted inside a timestamp-preserving UHD RF adapter:

```text
Downlink
CMX500 RF TX -> attenuation/isolation -> X310 RX2 -> UHD fc32
           -> GPU downlink channel -> srsUE PHY

Uplink
srsUE PHY -> GPU uplink channel -> UHD timed TX -> X310 TX/RX
          -> attenuation/isolation -> CMX500 RF RX
```

One X310 with a full-duplex UBX-160 daughterboard is sufficient for the first SISO FDD proof because its RX and TX can tune independently. It is sufficient **only** when this X310 is also srsUE's radio. A separate RF-to-RF bridge architecture would require another SDR on the srsUE side.

The present external ZeroMQ broker is not the recommended hardware path. Its IQ protocol carries samples but not UHD hardware timestamps, while the stock srsRAN ZeroMQ radio constructs a synthetic sample counter and paces receive calls with `usleep()`. That is acceptable for a virtual radio but unsafe for a CMX500 hardware loop: buffering and scheduler jitter can become an apparent RF delay, and uplink bursts can miss their timed-transmit deadlines.

The main feasibility risk is not GPU speed. It is whether the installed CMX500 software/options can expose an SA cell within the prototype srsUE feature subset. No official R&S–srsUE interoperability recipe was found. Prove direct CMX500–X310–srsUE attach before implementing the adapter. The current centered fractional-delay filter also needs the causal-boundary treatment described below before a fractional TDL result is valid on continuous RF.

## Scope and assumptions

This study assumes:

- the CMX500 is acting as the 5G network/callbox and exposes analog FR1 downlink and uplink RF ports;
- srsUE is the prototype 5G UE in `srsRAN_4G`, not a commercial handset;
- the first test is SA rather than NSA;
- the first carrier is n3 FDD, 20 MHz, 15 kHz SCS, 106 PRBs, one layer and one RF port per direction;
- the initial GPU channel is identity/pass-through, followed by fixed delay, path loss, AWGN, CFO and TDL tests;
- CMX500 internal fading and any other optional propagation impairment are disabled while the GPU owns the channel; and
- the X310 contains a UBX-160 or another daughterboard supporting the two n3 frequencies and simultaneous RX/TX.

This choice follows the documented srsUE SA limits: 15 kHz SCS, FDD, 5/10/15/20 MHz and no handover. The published srsRAN example uses n3, 20 MHz, 23.04 MS/s, 106 PRBs, one antenna and Release 15 settings. See the [srsUE 5G SA tutorial](https://docs.srsran.com/projects/project/en/latest/tutorials/source/srsUE/source/).

TDD, FR2, handover and wide MIMO are out of the first proof. NSA would also need a compatible LTE anchor and more RF paths, so it should be treated as a separate phase.

## Recommended hardware topology

### RF cabling

For a CMX500 configuration with separate RF TX and RX ports:

```text
                         downlink, about 1842.5 MHz
CMX500 DL TX  ----------------------------------------------> X310 UBX RX2
               fixed attenuator + optional limiter/DC block

                         uplink, about 1747.5 MHz
CMX500 UL RX  <---------------------------------------------- X310 UBX TX/RX
               fixed attenuator + optional isolator/DC block
```

The example frequencies form one convenient paired n3 allocation and must match the CMX500 carrier configuration. UBX-160 covers 10 MHz to 6 GHz and supports independent, simultaneous RX and TX; in full duplex its receive port is RX2 and its transmit port is TX/RX. See the [official UBX specification](https://www.ettus.com/all-products/ubx160/).

If the installed CMX500 exposes one combined TRX connector, use a band-3 duplexer/diplexer that has a common port and separate low-band UL and high-band DL ports:

```text
                                      high-band/DL -> attenuation -> X310 RX2
CMX500 TRX <-> common port [n3 duplexer]
                                      low-band/UL  <- attenuation <- X310 TX/RX
```

Follow the device's marked common/low/high ports; verify insertion loss, stop-band isolation, return loss, power rating and required unused-port termination. A generic circulator is not a drop-in substitute. Do not combine the two paths with a passive T-piece. Poor isolation can let the X310 transmitter desensitize or saturate its receiver.

Start with at least 30 dB of fixed attenuation in each cabled path, consistent with [srsRAN's X310 callbox precedent](https://docs.srsran.com/projects/4g/en/next/app_notes/source/5g_sa_amari/source/), but do **not** treat that Amarisoft example as a CMX500 safety specification or 30 dB as the final calculation. Before connecting RF, obtain the exact maximum input/output levels for the installed CMX500 RF unit and UBX revision, then calculate both normal and fault-case power. Add a limiter or more attenuation if a configuration mistake could exceed either input rating. The [CMX500 product brochure](https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_brochures_and_datasheets/product_brochure/5216_4127_12/CMX500_bro_en_5216-4127-12_v0800.pdf) confirms the FR1 RF-unit and port capabilities but does not replace the installed unit's safety manual.

### Clocking and host connection

Use a common 10 MHz laboratory reference for CMX500 and X310 when the installed CMX500 options expose a supported reference input/output. Never connect two reference outputs together. PPS or a supported trigger is strongly recommended for a repeatable epoch and latency measurements, though srsUE can acquire the frame boundary from the downlink waveform.

Configure the X310 master clock at 184.32 MHz, for which 23.04 MS/s is an exact integer-rate division. The X310 provides external reference/PPS connections, two daughterboard slots and 10GbE/PCIe host interfaces; see the [Ettus X300/X310 manual](https://files.ettus.com/manual/page_usrp_x3x0.html).

A dedicated 10GbE link is the qualification baseline. At 23.04 MS/s with sc16 over the wire:

| Quantity | Rate |
|---|---:|
| One complex sample | 32 bits |
| RX payload | 737.28 Mbit/s |
| TX payload | 737.28 Mbit/s |
| Aggregate full-duplex payload | 1.47456 Gbit/s |
| One fc32 host stream | 184.32 MB/s |
| RX + TX fc32 host streams | 368.64 MB/s |

A 1GbE link is uncomfortably close to its per-direction limit after packet and scheduling overhead. 10GbE provides the needed operating margin. Use a dedicated NIC, large socket buffers, CPU affinity and the performance governor. UHD's DPDK transport can be added if ordinary kernel networking shows latency spikes; the [UHD DPDK guide](https://files.ettus.com/manual/page_dpdk.html) describes its kernel-bypass and core-pinning model.

### Minimum equipment

| Item | Requirement |
|---|---|
| CMX500 | FR1 signaling unit, SA license/options, compatible R15-style cell profile |
| X310 | Current compatible FPGA image and UHD version |
| Daughterboard | UBX-160 or equivalent full-duplex coverage for n3 |
| Host | Linux workstation with NVIDIA GPU and dedicated 10GbE or supported PCIe/MXI path |
| RF protection | Calibrated coax, fixed attenuators, optional limiter/DC blocks/isolators |
| Duplexing | Separate CMX ports preferred; otherwise a rated n3 duplexer/diplexer with a documented port plan |
| Timing | Common 10 MHz reference; PPS/trigger where the installed CMX configuration supports it |
| Measurement | Power meter or spectrum analyzer for initial level and leakage calibration |

## Software architecture

### Preferred: an in-process srsUE RF adapter

Add an `ocudu_uhd` RF implementation of srsRAN's `rf_dev_t` interface. The interface already exposes receive timestamps, timed transmit, burst markers, radio time and asynchronous RF errors in [`rf.h`](https://github.com/srsran/srsRAN_4G/blob/6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16/lib/include/srsran/phy/rf/rf.h). The adapter should reuse the existing OCUDU `ChannelProcessor` and own one UHD `multi_usrp` instance.

The stock UHD path uses fc32 in host memory and sc16 over the wire. `ocg::IqSample` also stores two 32-bit floats, but matching size and field order alone do not make pointer reinterpretation alias-safe. The first implementation performs an explicit `std::complex<float>` to/from `IqSample` copy/conversion at the boundary and measures it. Replace that copy only after documenting a standards-valid common representation and testing it in the pinned compiler/ABI.

Proposed module boundary:

```text
srsUE
  |
  | rf_dev_t: recv_with_time / send_timed
  v
libsrsran_rf_ocudu.so
  +-- UHD RX/TX streams and metadata
  +-- DL ChannelProcessor state (CMX -> UE)
  +-- UL ChannelProcessor state (UE -> CMX)
  +-- persistent delay/timeline state and burst flags
  +-- RF/GPU deadline telemetry
```

srsRAN can load shared RF implementations, but the list of recognized plugin filenames is compiled into [`rf_dev.h`](https://github.com/srsran/srsRAN_4G/blob/6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16/lib/src/phy/rf/rf_dev.h). The integration therefore needs a small srsRAN patch and CMake target for `libsrsran_rf_ocudu.so`, or a built-in target. A standalone unmodified plugin file will not be discovered automatically.

The source audit in this study is pinned to srsRAN commit `6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16`. Use that revision for the first proof, or repeat the RF-ABI, plugin-discovery, TX-advance, continuous-TX and SA-timing audit before adopting another revision. Record the matching UHD version and X310 FPGA image.

### Proposed downlink and uplink pipelines

The first implementation keeps every stage on the X310/GPU/srsUE host. It uses one `multi_usrp`, one RX streamer, one TX streamer and independent `cmx_to_ue` and `ue_to_cmx` processor state. The X310 sample counter is the sole time coordinate. A typical execution quantum is one 23,040-sample, 1 ms slot, but UHD packets and RF API calls are allowed to fragment that range; packet boundaries never reset channel state.

#### Downlink: CMX500 to srsUE

```text
CMX500 NR DL RF
  -> fixed attenuation / isolation
  -> UBX RX2 analog receive chain
  -> X310 ADC + FPGA DDC/rate conversion
  -> UHD sc16 wire samples + RX metadata
  -> contiguous fc32 accumulator [S, S+N)
  -> H2D
  -> causal DL channel: TDL/delay -> path loss -> phase/CFO -> receiver AWGN
  -> D2H
  -> srsUE recv buffer carrying the original timestamp for sample S
  -> srsUE synchronization, FFT and UE PHY
```

| Stage | Owner | Required contract |
|---|---|---|
| DL0 RF acquisition | X310/UHD | Lock to the selected clock, stream continuously and emit the timestamp of the first ADC-derived output sample |
| DL1 assembly | RF adapter | Convert the first UHD time to absolute sample index `S`; collect exactly `N` contiguous samples despite partial receives |
| DL2 validation | RF adapter | Require every fragment to begin at the expected next tick; reject overflow, timeout, non-finite IQ or a gap/overlap |
| DL3 channel | DL `ChannelProcessor` | Process link `cmx0>ue0` with persistent history and the immutable P2 session epoch; later live epochs require X310-time integration |
| DL4 delivery | RF adapter | Return `N` processed samples labelled `[S, S+N)` and the original UHD time for `S`; never add GPU or queue time to the timestamp |
| DL5 telemetry | Adapter/backend | Record RX wait, fragment count, queue residence, H2D, kernel, D2H, total call time, sample range and active channel epoch |

The signal-processing order is intentional. TDL/common delay must lead the chain so the current CUDA fused path remains eligible and propagation history is applied before memoryless impairments. Path loss and phase/CFO shape the received link; receiver AWGN is added once after link superposition. For this SISO case the superposition has one input. The direction-specific treatment of acquisition and physical receiver noise is defined below.

The output sample at index `n` represents the channel output at X310 time `n`, even when it depends on an earlier input such as `x[n-D]`. The adapter therefore returns the acquisition timestamp unchanged. The current fractional-delay kernel may be used only with the common three-sample profile guard or after the streaming causal/lookahead fix described below.

#### Uplink: srsUE to CMX500

```text
srsUE UE PHY fc32 samples
  -> srsUE radio layer applies time_adv_nsamples once
  -> send_timed([S, S+N), SOB/EOB)
  -> contiguous sample-timeline validator
  -> H2D
  -> causal UL channel: TDL/delay -> path loss -> phase/CFO -> receiver AWGN
  -> D2H
  -> UHD timed partial-send loop at the unchanged sample index S
  -> X310 FPGA interpolation/DUC + DAC + UBX TX/RX chain
  -> fixed attenuation / isolation
  -> CMX500 NR UL RF receiver
```

| Stage | Owner | Required contract |
|---|---|---|
| UL0 scheduling | srsUE radio layer | Apply the single calibrated `time_adv_nsamples` and call the adapter with the resulting hardware time, payload and burst flags |
| UL1 timeline | RF adapter | Convert time to absolute index `S` with a half-sample rejection tolerance; accept only the expected next range |
| UL2 continuity | RF adapter | P2 accepts only the exact expected next range under continuous TX; any gap, overlap, backward time or undeclared discontinuity faults the session |
| UL3 channel | UL `ChannelProcessor` | Process link `ue0>cmx0` with independent FDD coefficients and persistent history; keep output labels `[S, S+N)` |
| UL4 deadline check | RF adapter | Measure `tx_slack_us` before processing and again before the first UHD send; fail if the remaining slack cannot cover the qualified UHD enqueue tail and margin |
| UL5 RF enqueue | UHD | Loop over partial sends with a hardware-time watchdog; timestamp/SOB only the first accepted fragment, preserve contiguous indices and attach EOB only to the final accepted sample; later burst mode extends this through the delayed tail |
| UL6 completion | UHD async monitor | Treat late command, underflow, sequence error or unexpected burst acknowledgement as a failed run |

The adapter must send at the timestamp supplied by srsUE. It must not subtract transport delay, channel delay, GPU wall time or a second hardware advance. The causal channel changes sample values on that timeline; it does not move the UHD transmit reservation. P2 forces continuous TX and requires contiguous calls, so every sample position advances UL history. Do not attempt to repair a newly discovered timestamp gap after its RF time has passed.

Burst mode is explicitly out of P2. Before enabling it, add backend contracts equivalent to `max_causal_tail_samples()`, `flush_tail()` and `advance_off_air_state(gap_samples)`. At EOB, the adapter must proactively process zero input and transmit all finite propagation tail samples before marking the last fragment EOB. A later SOB may advance time-varying state across the remaining off-air interval, but cannot retrospectively transmit samples. Receiver AWGN has no finite tail: either continuous RF carries the configured GPU noise, or an off-air policy disables GPU noise and relies on the physical receiver floor. Profile changes and new bursts are prohibited while a tail is pending.

#### State, buffering and concurrency

- Construct one direction-specific `ChannelProcessor` instance for DL and one for UL during adapter initialization. Call `prepare()` before streaming so topology and channel memory are preallocated; do not share delay, fading, CFO phase or AWGN RNG state between directions.
- Preallocate fixed-capacity fc32 assembly and output storage sized for the largest accepted RF call. Do not allocate, rebuild topology or retune the radio in a real-time call.
- Start P2 with one in-flight block per direction and synchronous completion. If continuous UHD draining later requires a background RX thread, use at most a two-slot preallocated handoff and expose its residence time and high-water mark. Queue saturation is a deadline failure, not permission to grow the queue.
- Keep the channel profile immutable during P2 and change P4 profiles only between stopped/re-armed test runs. The current runtime control uses a broker-local slot counter, not the X310 absolute sample index; live attached-radio changes remain disabled until the adapter maps the two timelines and tests the transition. A profile/delay change may reset history and expose a declared warmup interval, while scalar changes may preserve it. A call covering `[S, S+N)` must use one immutable epoch.
- Keep a separate UHD I/O/error path from the control path. Tune/gain/rate changes stop and re-arm streaming at a declared future epoch; they do not race an active data block.
- The adapter may copy between srsRAN, preallocated host and backend buffers initially. Add pinned memory or overlap only after P2 correctness, and count the actual H2D/D2H time rather than assuming zero-copy.

The current `ChannelProcessor` API processes sample spans and preserves per-link state, but it does not expose the burst-tail, off-air advancement or X310-absolute configuration interfaces above. Those are required extensions, not current capabilities. Continuous TX, immutable profiles and contiguous calls keep them off the P2 critical path.

#### Noise ownership and calibration

Use a receiver `rx_model` with explicit `noise_power` for calibrated RF tests. The current relative `snr_db` mode derives noise from each processing call's signal power, so under fading it tracks block power rather than representing a fixed thermal floor; retain it only for synthetic relative-SNR experiments.

On DL, X310 acquisition/conversion noise enters **before** the GPU channel and is shaped with the captured CMX waveform. With the CMX signal muted and GPU AWGN disabled, pass a representative acquisition capture through the exact intended channel profile and measure its post-channel contribution `P_x310_acq_after_channel`. The initial qualification gate requires that contribution to be at least 10 dB below `P_target_total_noise`; otherwise its channel-colored spectrum is material and the point must be re-levelled or rejected. When the gate passes, configure `P_gpu_noise = P_target_total_noise - P_x310_acq_after_channel` and verify total output noise within ±0.5 dB of target.

On UL, CMX500 receiver noise occurs physically after the GPU/X310 path. Measure it at the selected CMX bandwidth and gain, and configure only the additional GPU noise needed for the target total:

```text
P_gpu_noise = P_target_total_noise - P_measured_post_channel_physical_noise
```

If the result is not positive by more than calibration uncertainty, the requested noise/SNR point is infeasible at the current RF levels; change signal level/gain or reject the point. Do not clamp a negative result to zero and claim the target. Reference `P_target_total_noise` to a documented absolute normalized-IQ/RF-power mapping and measurement bandwidth. Under fading, keep the thermal floor fixed for the channel epoch rather than recomputing it from instantaneous faded signal power.

#### Fault and restart policy

Any RX timeout/discontinuity, non-finite input or output, queue saturation, missed TX guard, zero-progress TX watchdog, UHD late/underflow/overflow/sequence event or partial-send error moves the adapter to `FAULT`. In `FAULT` it must:

1. stop accepting RF API data and report the original cause plus the affected sample range;
2. stop and drain UHD RX/TX streamers and asynchronous events;
3. discard queued blocks and pending tails without pretending continuity;
4. destroy and `prepare()` fresh DL/UL processor instances, resetting delay/fading/CFO/AWGN state and expected indices; and
5. verify clock lock, arm a new future UHD epoch and require a fresh srsUE acquisition/attach.

Do not attempt live timeline resynchronization after an attached-radio continuity or deadline fault.

#### End-to-end local deadline

For a downlink range beginning at X310 time `t0`, the proposed local loop is:

```text
t0 .. t0+1 ms       X310 acquires one DL slot
after t0+1 ms        DL validation + GPU -> srsUE PHY -> UL GPU
before t0+4 ms-TA_time
                      UHD has accepted the complete timed UL range
```

Qualification uses the complete inequality in the delay section, not a per-kernel throughput result. The initial target is less than 250 us p99 for each direction's channel call, but acceptance requires the maximum observed processing/enqueue time to remain below the minimum observed TX slack with margin.

#### Optional `RU-lite` placement

If the system is later separated, the processing order remains the same and only the adapter boundary moves:

```text
DL: X310 RX -> gateway DL GPU -> timestamped IQ network -> remote srsUE
UL: remote srsUE -> timestamped IQ network -> gateway UL GPU -> timed X310 TX
```

The gateway remains the sole owner of both channel processors and the X310 clock. The remote adapter performs contiguous reassembly and forwards the original DL index or srsUE-produced UL index; it never runs another channel or retimestamps from arrival time. This optional form must pass R0–R3 and the two-network-crossing deadline below before use with the CMX500.

#### RF API receive contract

For each `recv_with_time(nsamples)` call:

1. Accumulate UHD receive fragments until exactly the requested sample count is available; do not assume one UHD call fills the request.
2. Preserve the first fragment's timestamp and require every later fragment to start at the exact next hardware tick. Reject timeout, overflow, bad packet sequence, non-finite samples or an unexpected discontinuity.
3. Apply the downlink `ChannelProcessor` with persistent history.
4. Return the processed samples to srsUE with **the original first-sample UHD timestamp**.
5. Record receive wait time, fragment count, GPU H2D/kernel/D2H time, total adapter time and sample continuity.

The processed sample at time index `n` remains labelled `n`. A configured causal channel—an integer delay, a guarded fractional filter or a redesigned causal fractional filter—produces `y[n] = x[n-D]`; it does not justify relabelling the block or adding GPU wall-clock time to the RF timeline.

#### RF API transmit contract

For each `send_timed(samples, tx_time, SOB, EOB)` call:

1. Convert `tx_time` to an absolute X310 sample index using UHD tick/rate conversion, with an explicit nearest-tick rule and a rejection tolerance of half a sample.
2. In P2 continuous-TX mode, require that index to equal the expected next sample; reject any gap, overlap or backwards time.
3. Reserve zero-input gap advancement and delayed-tail flushing for the later burst API described above; do not infer or repair missing past samples.
4. Apply the uplink channel while preserving persistent delay history.
5. Loop over partial UHD sends until every output sample is accepted. Before every attempt, re-read UHD hardware time and require enough remaining slack for the qualified enqueue guard. A zero-progress result may retry only before that guard expires; otherwise enter `FAULT`. The first accepted fragment carries the exact timestamp/SOB; subsequent fragments continue at the tracked next sample; only the final fragment carries EOB.
6. Send at exactly the timestamp passed into the adapter; do not apply another RF hardware advance there.
7. Preserve start/end-of-burst semantics. P2 stays continuous; future burst support must use the explicit tail contract and must not discard delayed samples after EOB.
8. Consume UHD asynchronous TX events and fail loudly on a late command, underflow, sequence error or unexpected burst acknowledgement.

The first proof forces continuous TX, which removes burst-gap and finite-tail behavior from the active implementation. Later discontinuous operation remains blocked on the explicit tail/off-air APIs and noise policy above.

The key runtime metric is:

```text
tx_slack_us = requested_tx_hardware_time - current_uhd_hardware_time
```

Measure it before channel processing and immediately before the UHD send. The adapter is viable only if the minimum observed slack remains above the measured processing/driver tail latency with margin.

#### Channel ownership

Use separate state for `cmx_to_ue` and `ue_to_cmx`. FDD means the two directions use different carrier frequencies and may have different channel coefficients. The initial topology is one source, one destination and one link in each direction; later MISO/SIMO work can reuse the port-aware roadmap in [the MIMO integration report](../mimo-integration-report.html).

Keep these quantities separate in configuration and telemetry:

| Quantity | Meaning | Treatment |
|---|---|---|
| `transport_calibration_samples` | fixed ADC/DAC, FPGA, UHD and cable skew | measured and reported; not a propagation tap |
| `time_adv_nsamples` | how early srsUE launches TX to align at RF | measured once and applied by the srsUE radio layer before the adapter call |
| `channel_common_delay_samples` | intended propagation time of flight | causal GPU delay |
| `excess_tap_delays` | multipath relative to the chosen common delay | causal GPU TDL state |
| `fractional_filter_guard_samples` | fixed group delay needed to make the centered fractional FIR causal | three samples for the current 8-tap filter when using the guard strategy; declared separately |
| GPU/host wall time | time spent computing and moving samples | deadline consumption, not modeled propagation |

Use one TX-advance owner. The recommended design keeps `time_adv_nsamples` in srsUE, whose radio layer adjusts the timestamp before calling `send_timed`; the adapter forwards that timestamp unchanged to UHD. If advance is ever moved into the adapter, set the srsUE value to zero. Never apply both. Do not hide GPU latency in TX advance, and do not encode radio transport calibration as a TDL tap.

### Alternative architectures

| Architecture | Feasibility | Timing quality | Hardware | Decision |
|---|---|---|---|---|
| In-process UHD/GPU RF adapter | Conditional until P1 and deadline gates pass | Best: preserves hardware timestamps and timed TX | One X310 | **Recommended path** |
| External UHD gateway -> current ZMQ broker -> stock srsUE ZMQ | Prototype only | Poor: no hardware time, sequence or delivery-window contract | One X310 | Use only for offline/early IQ checks |
| Timestamped time-domain `RU-lite` gateway | Plausible after the in-process proof | Conditional on measured network PDV and UL slack | One X310; dedicated 10/25GbE | Future isolation/disaggregation option |
| Literal O-RAN 7.2x O-RU/O-DU split | Not a practical srsUE integration | Strong fronthaul discipline but wrong PHY boundary | New UE lower-PHY implementation | Do not use for the first program |
| CMX500 -> bridge X310 -> GPU -> second srsUE SDR | Possible | More converter, clock and cable delay to calibrate | Two SDRs | Avoid for first proof |

The current srsRAN ZMQ implementation assigns timestamps from `next_rx_ts` and paces the receiver by sleeping for the nominal sample duration in [`rf_zmq_imp.c`](https://github.com/srsran/srsRAN_4G/blob/6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16/lib/src/phy/rf/rf_zmq_imp.c). Its wire exchange contains raw samples, not UHD timestamp metadata. An external gateway could eventually work only after defining an absolute-sample/timestamp protocol, making the hardware clock the pacing authority, and buffering timed uplink sufficiently ahead. That is more work and more jitter than the RF-adapter design.

## What an RU–DU split contributes

### Decision: reuse the contracts, not the 7.2x functional split

RU–DU separation gives this design a useful model for transport ownership, timing and observability. It does **not** provide a ready-made srsUE interface. O-RAN 7.2x places FFT/iFFT, cyclic-prefix handling and other lower-PHY work in the O-RU and exchanges frequency-domain resource elements with the O-DU. srsUE is a UE rather than an O-DU, and it has no O-RAN 7.2x endpoint. The [O-RAN fronthaul C/U/S specification](https://www.etsi.org/deliver/etsi_ts/103800_103899/103859/17.01.00_60/ts_103859v170100p.pdf) is therefore an architectural reference, not an interoperability target.

The physical channel must remain on time-domain IQ for this experiment. Common sample delay, multipath across a cyclic-prefix boundary, CFO, phase noise, timing error, PRACH behavior and later RF impairments are naturally applied to the waveform before srsUE synchronization and FFT processing. Moving the boundary to frequency-domain resource elements would either remove those effects or require a new NR UE lower PHY, which is disproportionate to the proof. It would also invalidate the goal of testing the existing srsUE synchronization and RF path through the emulator.

The closest useful analogy is therefore an Option-8-like raw-IQ radio head, called `RU-lite` here to avoid claiming standards compliance:

```text
CMX500 analog RF
      |
      v
X310/UHD + hardware-clock owner + DL/UL GPU channel state   (RU-lite gateway)
      |
      | timestamped time-domain IQ; bounded delivery windows
      v
srsUE RF adapter + UE PHY                                  (baseband side)
```

Keep the radio path local through P5: P1 uses stock UHD, while P2–P5 use the in-process adapter. Consider the network boundary only if process isolation or a second host becomes a requirement. In the selected `RU-lite` mode, the gateway host owns the X310, UHD and both `cmx_to_ue` and `ue_to_cmx` GPU `ChannelProcessor` states. The remote srsUE adapter only transports timestamped IQ; it must not apply a second channel. This placement keeps UHD/GPU transfers under one scheduler and one hardware-time owner, although both network directions still consume the end-to-end FDD deadline.

### RU-inspired protocol contract

The useful O-RAN/eCPRI lesson is to make radio time and delivery windows first-class protocol data instead of deriving them from request/reply cadence. The [eCPRI 2.0 specification](https://www.cpri.info/downloads/eCPRI_v_2.0_2019_05_10c.pdf) defines IQ-data flows, sequence identity, real-time control, one-way-delay measurement and equipment reference points. A future OCUDU transport can borrow those semantics without adopting its wire format.

| Concern | Minimum `RU-lite` contract |
|---|---|
| User plane | IQ payload plus protocol version, run/epoch ID, direction, stream/port ID, sequence number, 64-bit first-sample index, sample count, sample rate, format and discontinuity/SOB/EOB flags |
| Control plane | Tune, sample rate, gain, stream start/stop, epoch arming and channel-profile changes; every timed change carries an effective absolute sample index |
| Synchronization plane | X310 clock/time source, lock state, UHD epoch mapping, 10 MHz/PPS state and optional PTP quality; the X310 sample counter remains authoritative |
| Management/telemetry | Capabilities, calibrated fixed delays, configured delivery windows, queue occupancy, packet gaps/duplicates, late/early counts, UHD overflow/underflow and TX slack |

Add an integrity check at least over the header and payload. Frame, slot and symbol identifiers may be diagnostic metadata after cell timing is known, but they must not replace the absolute sample index: srsUE must first search and synchronize from an arbitrary point in the waveform. Packet size is also independent of the 1 ms channel-processing block. Use MTU-appropriate fragments, preserve each fragment's sample range, then reassemble exact contiguous ranges for the current `ChannelProcessor` call.

The sole TX-advance rule extends across this boundary. srsUE applies `time_adv_nsamples` before its RF-adapter send and releases the resulting absolute first-sample index. The baseband transport and gateway forward that index unchanged. The gateway must not subtract transport calibration, TX advance or GPU wall time, and it must never retimestamp a block from its network-arrival time.

Do not silently synthesize continuity after a lost, duplicate, overlapping or late packet. Raise a discontinuity event and fail the attached-radio run unless an explicit test policy requests zero insertion. Transport packets also do not solve the current centered fractional FIR's future-sample read: fractional profiles still require the declared common three-sample guard on **every** tap in the profile, or a validated streaming-lookahead/causal-kernel redesign.

Compression is not required for the first SISO transport. At 23.04 MS/s, sc16 is 737.28 Mbit/s per direction and 1.47456 Gbit/s aggregate; cf32 is 1.47456 Gbit/s per direction and 2.94912 Gbit/s aggregate before headers. Both fit a dedicated 10GbE link. Any later quantization or compression must be treated as an EVM/channel-calibration change, not merely a bandwidth optimization.

### Timing-window model

O-RAN defines transmit and receive windows rather than accepting an average packet latency. Apply the same idea at the time-domain boundary:

```text
gateway TX window  ===== samples leave =====>
network PDV             <---- variation ---->
baseband RX window          [all samples must arrive]

baseband UL call       ===== timestamped IQ =====>
gateway deadline                              [GPU + UHD timed TX]
```

Define windows as intervals in one PTP-correlated measurement clock, while retaining the X310 sample index as the data-plane authority. For a sample range, let sender egress occur within release interval `R = [r_min, r_max]`; let calibrated one-way transport lie in `[d_min, d_max]`; let the maximum clock-mapping error be `epsilon`; and let the receiver accept arrivals in `A = [a_min, a_max]`. The configured window must contain every qualified arrival:

```text
a_min <= r_min + d_min - epsilon
a_max >= r_max + d_max + epsilon

therefore:
width(A) >= width(R) + (d_max - d_min) + 2 * epsilon
```

Record the fixed offset `d_min`, the PDV bound `d_max - d_min`, the clock-error allowance and the observed earliest/latest arrival separately. An arrival before `a_min` is an early-window violation and may be buffered only if declared capacity exists; an arrival after `a_max`, or a sequence still missing at `a_max`, is a hard late/discontinuity failure. For uplink, derive `a_max` backwards from the requested X310 TX time so the remaining gateway GPU and UHD-enqueue bound plus safety margin still fits. Set `a_min` from the finite jitter-buffer capacity rather than accepting arbitrarily early data.

For uplink, the decisive condition at the moment srsUE releases a timed block is:

```text
remaining_tx_slack
  > network_tail + reassembly_tail + GPU_tail + UHD_enqueue_tail + safety_margin
```

Use a declared high-tail or hard qualification bound, not the mean. Log minimum/maximum/percentile one-way delay, queue depth and the earliest and latest packet in every sample window. A bounded jitter buffer may absorb approved PDV, but its occupancy is deadline consumption; it must never be reported as modeled propagation delay or used to relabel the samples.

The separated path must also close the complete FDD loop. From the first sample of a 1 ms downlink receive call, the conservative condition is:

```text
1 ms RX acquisition
+ DL GPU tail + DL packetization/network/reassembly tail
+ srsUE PHY tail
+ UL packetization/network/reassembly tail + UL GPU tail
+ UHD enqueue tail + safety margin
< 4 ms - timing_advance_time
```

Measure `tx_slack_us` twice: when the srsUE adapter releases the uplink range and at the gateway immediately before its first timed UHD send. The difference exposes transport, reassembly and gateway processing consumption; the second value must still exceed the measured UHD-enqueue tail plus safety margin.

A common 10 MHz/PPS reference is the RF-frequency/epoch basis when available. PTP can correlate the two hosts and support one-way-delay measurements, but it neither guarantees sample continuity nor removes operating-system/network jitter. SyncE and full O-RAN synchronization profiles are unnecessary for the first same-rack proof; clock lock, sample index and UHD time are the controlling truths.

Calibrate delay at named reference points so transport does not leak into the channel model:

1. X310 RF connector;
2. UHD first-sample timestamp;
3. gateway network egress/ingress;
4. srsUE RF-adapter ingress/egress; and
5. CMX500 RF connector.

This is the most valuable eCPRI analogy: fixed equipment/transport delay, packet-delay variation and intended channel delay remain separately measurable quantities.

### Optional disaggregation gates

Do not make the networked gateway a dependency of the initial CMX500 proof. Qualify it later in four bounded steps:

1. **R0 — protocol record/replay:** prove header validation, sequence/discontinuity handling, exact sample-index reconstruction and timed control changes without live RF.
2. **R1 — same-host process split:** compare the gateway against the in-process adapter with identity and integer-delay sweeps; require identical sample alignment and no synthetic pacing.
3. **R2 — two-host dedicated Ethernet:** run bidirectional load, injected jitter and loss tests; pass only with zero continuity errors and sufficient measured minimum UL `tx_slack_us` for the declared delivery window.
4. **R3 — attached CMX500:** repeat identity attach, RF calibration and impairment gates. Stop on any late UHD command, underflow, overflow, timestamp discontinuity or unexplained EVM regression.

A literal O-RAN 7.2x implementation should be reconsidered only if the project later targets an actual O-DU/O-RU ecosystem and is willing to implement a standards-conformant lower PHY. It does not reduce the risk of the present CMX500–srsUE waveform loop.

## Delay and deadline analysis

### Four delays must not be conflated

1. **Modeled common propagation delay**: intentional sample delay between the virtual transmitter and receiver.
2. **Modeled excess multipath delay**: relative delay of each TDL component.
3. **Fixed RF transport/group delay**: ADC, DAC, filters, FPGA, UHD packetization, cable and duplexer delay.
4. **Compute/queue latency and jitter**: wall time used by UHD, CPU scheduling, CUDA copies/kernels and srsUE processing.

The first two define the requested virtual channel impulse response. If the three-sample fractional-filter guard is selected, it appears in the realized impulse response as an implementation-induced common shift; report it separately and validate the device maximum after applying it. Fixed RF transport delay is calibrated outside the channel model. Compute time consumes a real-time deadline but does not move the sample timestamp when the adapter preserves UHD metadata.

### Available scheduling slack

In the current srsUE SA path, the timestamp returned with a received slot is advanced by `FDD_HARQ_DELAY_DL_MS` and reduced by timing advance before the worker's transmit time is set; see [`sync_sa.cc`](https://github.com/srsran/srsRAN_4G/blob/6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16/srsue/src/phy/sync_sa.cc). That constant is 4 ms in [`common.h`](https://github.com/srsran/srsRAN_4G/blob/6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16/lib/include/srsran/common/common.h).

This is not a 4 ms GPU budget. PHY work, operating-system jitter, UHD enqueueing and CMX timing already consume part of it. It does show why a roughly 100–300 microsecond channel operation may be practical if timestamps are preserved and tail latency is controlled. The actual gate is measured `tx_slack_us`, not the source-code constant.

At 15 kHz SCS the receive call normally gathers a 1 ms, 23,040-sample slot. If the returned timestamp identifies the first sample, as the RF API convention requires, that acquisition has already consumed about 1 ms of the 4 ms offset. A conservative deadline test is therefore:

```text
1 ms RX slot acquisition
+ RX adapter/channel tail latency
+ srsUE PHY tail latency
+ TX adapter/channel tail latency
+ UHD/network enqueue tail latency
+ safety margin
< 4 ms - timing advance
```

Instrument each term rather than deriving a pass from average throughput. The timeline should look like:

```text
X310 time       t0             t0+1 ms                         t0+4 ms-TA
                 | RX slot ------>| channel -> PHY -> channel ->| timed UL RF
deadline state                    work must complete before -----^
```

A configured channel delay `D` does not add a `D`-second sleep to this path. Persistent causal history makes the output at sample `n` depend on input `n-D`, while the output remains on the X310 sample timeline. Only the computation needed to produce that output consumes deadline slack.

The stock srsRAN calibration uses 45 samples for a device named `uhd_x300` in [`radio.cc`](https://github.com/srsran/srsRAN_4G/blob/6bcbd9e5bf8686aa7085202cd847c5ddd64a9c16/lib/src/radio/radio.cc). A new adapter name would not automatically inherit that value. Use a manually measured `time_adv_nsamples` initially; at 23.04 MS/s, 45 samples are about 1.953 microseconds, but that stock number does not include the new adapter or lab RF path.

### Sample-time scale at 23.04 MS/s

| Samples | Time |
|---:|---:|
| 1 | 43.403 ns |
| 8 | 0.347 us |
| 45 | 1.953 us |
| 128 | 5.556 us |
| 300 | 13.021 us |
| 1,024 | 44.444 us |
| 2,048 | 88.889 us |
| 4,096 | 177.778 us |
| 23,040 | 1.000 ms |

The current CUDA device representation supports at most 32 taps and a 128-sample delay-history ring, as defined in [`device_channel.h`](../../include/ocudu_gpu_channel/device_channel.h). The ring must also hold the 8-tap fractional-delay filter span, so the largest accepted tap is approximately 120 samples, or 5.21 microseconds at 23.04 MS/s, rather than the full 128 samples. This is enough for initial indoor TDL profiles but not for arbitrary long-distance propagation or a large artificial common delay. Extend the ring or adopt the planned pre-kernel delay design before claiming longer delays; never turn processing latency into a substitute.

### Fractional-delay block boundary

The current 8-tap Hamming-windowed sinc is centered. Its convolution index in [`delay.h`](../../include/ocudu_gpu_channel/delay.h) includes `+3`, and an index beyond the current call's input block is replaced with zero. A fractional tap whose integer delay is below three samples can therefore need up to three samples from the next block; the final samples of every call are otherwise distorted. Integer-delay identity/delay sweeps are unaffected in practice because the centered integer coefficient selects the available sample, but an arbitrary fractional TDL is not yet a clean continuous-stream claim.

For the first attached fractional-TDL proof, use one of these explicit strategies:

1. **Causal guard, recommended for the PoC:** for any profile containing fractional delays, add the same three-sample common offset to every tap, including integer-valued taps. Declare `fractional_filter_guard_samples = 3` and include the resulting 130.2 ns as fixed algorithmic channel delay. This preserves the profile's relative tap delays; ensure the largest guarded tap still fits the approximately 120-sample device limit.
2. **Preserve sub-three-sample absolute delay:** extend the processor/adapter with lookahead that does not advance FIR/fading state for the lookahead samples, or replace the interpolator with a validated causal streaming fractional-delay design. The TX implementation must still produce every timed sample before its UHD deadline.

Whichever strategy is selected, process the same long vector once and in variable-sized partitions and compare the complete output. The result must not change at partition boundaries, and an RF EVM trace must not show a periodic spike once per 1 ms call. P4 is blocked until this test passes.

3GPP timing-advance encoding is not the same as the end-to-end tolerance of this setup. For 15 kHz SCS, the NR random-access timing-advance command has a nominal step of about 0.521 microseconds and an encoding range of roughly 2.003 ms under the formula in [ETSI TS 138 213](https://www.etsi.org/deliver/etsi_ts/138200_138299/138213/18.08.00_60/ts_138213v180800p.pdf). Treat that only as an encoding ceiling. The CMX500 scheduler, srsUE acquisition/PRACH behavior and implementation buffers may fail much earlier. Determine the usable limit with a fixed-delay sweep.

### Existing GPU evidence

On this project's RTX 5090 performance run, the full `model_mix_latency` p99 for one TDL-A link was 122 microseconds and for 16 edges was 319 microseconds; see [the device-channel measurements](device-channel-pipeline.md#d3-result--tdl-profiles-are-realtime-fit-for-the-first-time). These figures are encouraging but do not include UHD receive/transmit, a CMX500, srsUE PHY scheduling, plugin overhead or host contention.

Use the following provisional engineering gates for the first SISO integration:

| Metric | Initial gate |
|---|---|
| GPU/channel adapter p99 per direction | < 250 us |
| UHD RX/TX errors | zero late/underflow/overflow/sequence errors |
| Sample continuity | zero unaccounted gaps, overlaps or slips |
| Timestamp mapping | exact integer-sample continuity |
| TX deadline | minimum slack greater than maximum observed adapter/UHD enqueue time over qualification plus margin; p99/p99.999 remain diagnostics |
| Long-run latency | bounded; no monotonic queue growth |

These are project targets, not CMX500 or 3GPP specifications. Tighten them using the minimum slack measured in the bypass test.

Use p99 while tuning, but do not qualify a timed-radio path from p99 alone. The P2/P5 decision uses the minimum TX slack and maximum observed adapter latency over the endurance run, supplemented by a high-confidence tail percentile such as p99.999 once the run contains enough samples.

## Initial radio configuration

### CMX500

Create the simplest srsUE-compatible cell available in the installed CMsquares/CMX software:

| Setting | Baseline |
|---|---|
| Mode | 5G NR SA signaling |
| Duplex | FDD |
| Band | n3 |
| Bandwidth/SCS | 20 MHz / 15 kHz |
| RF | one downlink port, one uplink port, SISO/rank 1 |
| Example center frequencies | DL 1842.5 MHz, UL 1747.5 MHz |
| UE capability expectation | Release 15, conservative feature set |
| Search spaces/PRACH | common/fallback configuration compatible with the srsUE tutorial |
| Subscriber | PLMN, IMSI, K, OPc and APN/DNN matching `ue.conf` |
| Internal RF effects | fading, AWGN and other optional impairments off for identity tests |

The CMX500 supports FR1 up to 8 GHz and SA/NSA signaling according to the [R&S product documentation](https://www.rohde-schwarz.com/products/test-and-measurement/wireless-tester-network-emulators/rs-cmx500-radio-communication-tester_63493-601282.html). Exact menu names, installed licenses and permitted cell parameters must be checked on the actual instrument.

### X310/UHD

Use settings equivalent to:

```text
type=x300
addr=<dedicated-10gbe-address>
master_clock_rate=184.32e6
clock_source=external          # only after the reference is verified
time_source=external           # when PPS/trigger is connected and supported
otw_format=sc16
rx_subdev=<UBX channel>
tx_subdev=<same UBX channel>
rx_antenna=RX2
tx_antenna=TX/RX
rx_rate=23.04e6
tx_rate=23.04e6
rx_freq=1842.5e6
tx_freq=1747.5e6
```

Set RX and TX gains manually after power calibration. Do not enable AGC during timing/level qualification. Start continuous RX with a future UHD time after clocks lock; use future timestamps for every uplink burst. UHD documents timed stream starts, RX metadata timestamps and timed TX in its [timed-command guide](https://files.ettus.com/manual/page_timedcmds.html).

### srsUE

The final syntax depends on how the adapter is integrated, but the intended configuration is:

```ini
[rf]
device_name = ocudu_uhd
device_args = type=x300,addr=<addr>,master_clock_rate=184.32e6,...
srate = 23.04e6
nof_antennas = 1
time_adv_nsamples = <measured, not auto>
continuous_tx = yes

[rat.eutra]
nof_carriers = 0

[rat.nr]
nof_carriers = 1
bands = 3
max_nof_prb = 106
nof_prb = 106

[rrc]
release = 15
```

Start from the current official SA tutorial's `ue.conf`, then change only the RF device and the credentials needed to match the CMX500. Keep the soft-USIM values synchronized with the callbox subscriber database. Force continuous TX for the first proof so no timestamp gap exists; idle waveform positions are explicit zero-valued samples and the uplink channel history advances continuously. Burst gaps and tails remain disabled until the required backend interfaces exist.

## Calibration procedure

### 1. RF safety and level mapping

With the X310 transmitter disabled:

1. Verify cable loss and attenuator values with a VNA/power meter where available.
2. Set the CMX500 downlink to a low known output level.
3. Increase X310 RX gain conservatively while monitoring fc32 RMS, PAPR and clipping count.
4. Choose a fixed gain that leaves at least the intended waveform headroom.
5. Disable the CMX500 downlink, enable a low X310 uplink test waveform, and verify received power at the CMX500.
6. Record the equations mapping CMX500 dBm to normalized RX IQ and normalized TX IQ to CMX500 dBm.

The GPU path-loss and AWGN controls are meaningful only after these mappings are stable. Otherwise a gain change in UHD can masquerade as a channel-model change.

### 2. Clock/CFO calibration

Confirm that X310 reports external-reference lock before streaming. With the GPU in identity mode, measure residual downlink and uplink CFO. If a common reference is unavailable, characterize drift over at least 30 minutes and verify that srsUE and CMX500 estimators tolerate it; do not assume acquisition once implies long-run stability.

### 3. Fixed transport delay

Define the measurement points before assigning a delay to a direction:

- **A:** the timestamp srsUE passes into the adapter;
- **B:** the UHD ADC/DAC hardware timestamp;
- **C:** the waveform crossing the X310 RF connector, observed with a shared-reference receiver/oscilloscope; and
- **D:** the CMX500's reported uplink arrival/timing error or a time-aligned CMX trigger/capture.

An attenuated timed X310-TX-to-X310-RX loopback measures combined TX+RX transport and repeatability, but it cannot separate the directions by itself. Measure a direction only when a reference observation at C or D makes that direction identifiable—for example, split a known CMX downlink to a synchronized reference receiver and the X310 for RX delay, then split a timed X310 uplink to the CMX500 and reference receiver for TX delay. If that equipment or CMX trigger/report is unavailable, report only effective end-to-end calibration and do not label it as two directional delays.

Repeat the measurement across restarts and gains. Store fixed transport calibration separately from modeled channel delay. Calibrate `time_adv_nsamples` by minimizing uplink timing error at the CMX500 while the GPU channel is identity. Apply that value only in srsUE; verify from adapter telemetry that the timestamp passed to UHD is unchanged.

### 4. Delay sweep

Apply integer delays of 0, 8, 16, 32, 64, 96 and 120 samples in three explicit cases: downlink only with uplink at zero, uplink only with downlink at zero, and the same delay in both directions. These values remain inside the current ring because an integer tap does not need the fractional-filter guard. At each point record:

- cell search and synchronization result;
- PRACH detection and random-access completion;
- RRC registration and PDU session result;
- commanded/observed timing advance;
- BLER and throughput;
- UHD errors, adapter p50/p99/max latency and minimum TX slack; and
- sample-gap/overlap counters.

The pass/fail edge from this sweep is the real CMX500–srsUE delay budget for this profile.

## Staged execution plan and gates

### P0 — inventory and configuration feasibility

- Record CMX500 model, RF-unit type, firmware, installed signaling/fading licenses, port map, reference connectors and safe power limits.
- Confirm X310 FPGA/UHD compatibility, UBX revision and 10GbE link.
- Confirm the CMX500 can create the baseline n3 SA R15-style cell.
- Export or screenshot the complete CMX500 cell, RF-route, subscriber, authentication, CORESET/search-space and PRACH configuration; the baseline table above is a compatibility envelope, not a ready-to-import CMsquares recipe.

**Gate:** all RF connections are power-safe and the requested cell is expressible. Stop if the instrument cannot produce a profile inside the srsUE subset.

### P1 — direct RF bypass

Run stock srsUE with the stock UHD driver and X310, with no GPU adapter. Achieve cell search, PRACH, RRC registration, PDU session, ping and sustained traffic. Record CMX500 and srsUE logs plus UHD errors.

**Gate:** repeatable direct attach and at least 10 minutes of traffic with zero UHD late/overflow/underflow errors. This isolates CMX500/srsUE interoperability from channel-emulator engineering.

### P2 — timestamp-preserving identity adapter

Implement the `rf_dev_t` wrapper, reuse the current `ChannelProcessor`, preserve receive timestamps and timed transmit, and run an identity/noise-free channel.

**Gate:** all P1 functions still pass; no sample discontinuity; no UHD error; bounded adapter latency; minimum TX slack exceeds the measured tail plus margin.

### P3 — calibrated fixed delay

Calibrate transport delay and TX advance, then run the fixed-delay sweep. Verify that an injected impulse or correlation sequence moves by exactly the requested samples and that GPU wall time does not appear as extra modeled delay.

**Gate:** sample-accurate injected delay, stable random access and a documented operating envelope.

### P4 — channel impairments

Add one effect at a time: path loss, calibrated absolute-power AWGN, phase/CFO and finally a static then fading TDL. Keep CMX500 optional fading/AWGN off. Before AWGN, pass the direction-specific acquisition/physical-noise feasibility calculation. Before a fractional TDL, implement the selected causal-boundary strategy and pass the one-shot-versus-partitioned-stream test. Compare expected and measured power, SNR, delay profile, BLER and throughput.

**Gate:** each effect has an independent oracle or measurement; no double application of loss/noise/fading; no output change or periodic EVM artifact at a processing-block boundary.

### P5 — endurance and reproducibility

Run 30–60 minutes at the intended traffic load, then repeat from a cold start. Capture configurations, software commits, FPGA/UHD versions, calibration values and latency histograms.

**Gate:** no radio errors or sample slips, no queue growth, stable KPI distributions and repeatable attach.

## Implementation work breakdown

| Work item | Estimate | Main uncertainty |
|---|---:|---|
| CMX500 inventory, RF safety and direct attach | 1–3 days | Installed licenses/profile compatibility |
| srsRAN RF plugin target and UHD wrapper | 3–5 days | Build/package integration with srsRAN |
| DL/UL `ChannelProcessor` integration and continuous sample timeline | 3–5 days | Partial transfers, persistent state and timed-TX guard |
| Optional burst/off-air backend interfaces | 2–5 days after P2 | Tail definition, noise policy and time-varying state advancement |
| Telemetry, timestamp tests and loopback calibration | 2–4 days | Access to suitable measurement equipment |
| CMX500 delay/impairment/endurance qualification | 3–7 days | Callbox behavior and real-time tail latency |

A focused direct-attach plus identity-channel proof is roughly a 1–2 week task once the equipment and CMX500 profile are ready. A qualified impairment path with calibration and endurance evidence is more realistically 3–5 weeks for one engineer. These are engineering estimates, not commitments; P1 can terminate the effort early if the CMX500 and srsUE profiles are incompatible.

## Required telemetry

Emit one structured record per RX/TX block and aggregate histograms for:

- first-sample UHD timestamp and absolute sample index;
- expected versus actual next timestamp;
- requested TX time, UHD time at entry/send, and TX slack;
- RX wait, H2D, kernel, D2H, total channel and total RF-call latency;
- block size, SOB/EOB and continuity-fault counts; if later enabled, separately report off-air advancement and flushed-tail samples;
- ADC clip count, IQ RMS/peak/PAPR and configured RX/TX gain;
- UHD late, underflow, overflow, timeout and sequence counters;
- active channel epoch/configuration hash and common/excess delays; and
- CMX500-visible timing advance, BLER, RSRP/SINR and throughput when exportable.

UHD supplies `benchmark_rate`, latency and timed-TX examples; use them before srsUE integration and require a clean result with no dropped, overflowed, underflowed or late samples. See the [UHD example guide](https://files.ettus.com/manual/page_gsg_examples.html) and [streaming test notes](https://files.ettus.com/manual_archive/v3.11.0.1/html/page_rdtesting.html).

## Risks and stop conditions

| Risk | Consequence | Control |
|---|---|---|
| CMX500 feature profile exceeds srsUE R15 subset | UE never completes attach | Conservative n3 SA profile; direct-bypass P1 first |
| Unsafe RF level or poor duplex isolation | Hardware damage or RX saturation | Rated attenuation/limiting, port-manual review and measured leakage |
| Lost/synthetic timestamps | Channel latency becomes timing error | In-process timestamp-preserving adapter |
| GPU/OS tail latency misses UHD timestamp | Late TX, failed PRACH/HARQ | Measure minimum slack, pin resources, 10GbE/DPDK if needed |
| Delay ring too short | Longer channel rejected or corrupted | Enforce the ring-minus-filter/guard bound—approximately 120 tap-delay samples today—and revalidate after the common guard; extend representation before longer tests |
| Centered fractional FIR reads beyond a call boundary | Periodic corruption in low-delay fractional taps | Three-sample causal guard for the PoC, or a validated lookahead/causal interpolator before P4 |
| Burst gaps/EOB mishandled | FIR state discontinuity or lost delayed tail | Continuous contiguous P2 only; require tail query/flush, off-air advancement and noise-policy tests before burst mode |
| UHD returns partial RX/TX transfers | Missing or duplicated samples despite a successful call | Accumulation/send loops with per-fragment timestamp checks and timeouts |
| TX advance applied in both srsUE and adapter | Uplink is launched too early | srsUE is the sole advance owner; adapter forwards its timestamp unchanged |
| Networked `RU-lite` PDV consumes the UL deadline | Correct IQ arrives too late for timed RF | Keep P1–P5 in-process; qualify absolute-sample transport and delivery windows through R0–R3 before attachment |
| Double fading/noise/path loss | Invalid channel claims | Single owner per impairment; CMX effects off during GPU tests |
| Acquisition/physical noise already exceeds the target floor | Requested SNR cannot be synthesized by adding GPU noise | Calibrate each direction in absolute power and reject or re-level infeasible points |
| Broker slot epoch is mistaken for X310 time | Live profile changes occur at the wrong RF sample | Keep P2/P4 attached profiles immutable per run until an absolute-sample mapping and transition test exist |
| Clock/reference misconfiguration | CFO/drift and intermittent loss of sync | Common reference, lock checks and long-run CFO measurement |
| Automatic TX advance selects zero/wrong value | Uplink timing offset | Manual loopback-calibrated value for the new device name |

Stop or redesign if any of the following remains true after the relevant gate:

- the CMX500 cannot expose a compatible SA/FDD/15 kHz profile;
- safe port levels or isolation cannot be established from the installed manuals and measurements;
- timestamped TX slack is smaller than the adapter's measured tail latency;
- UHD errors or sample slips occur under the identity channel;
- a common or sufficiently stable reference cannot keep residual CFO within acquisition/tracking capability; or
- the deployment requires only the current unmodified ZMQ timing model.

## Final recommendation

Proceed, but in this order:

1. inventory the installed CMX500/X310 options, calculate safe levels and configure the conservative n3 SA cell;
2. cable the X310 with measured attenuation, independent FDD ports or the specified n3 duplexer, and a verified common reference;
3. prove stock UHD srsUE attach and record the complete passing CMX500/srsUE configuration;
4. implement `libsrsran_rf_ocudu.so` as a UHD-owning, timestamp-preserving RF adapter;
5. start with a GPU identity channel and manual transport/TX-advance calibration;
6. qualify deadline slack, integer delay and fractional-delay boundary continuity before enabling stochastic channel effects; and
7. use the current ZMQ broker only for non-hardware experiments unless its protocol and pacing are explicitly redesigned around hardware timestamps; and
8. if later separation is required, build the timestamped time-domain `RU-lite` gateway and pass R0–R3 rather than moving the experiment to an O-RAN 7.2x frequency-domain boundary.

The current 122 microsecond project measurement suggests the GPU has enough performance for the first SISO path. The feasibility decision nevertheless remains conditional until the direct CMX500 attach and measured minimum TX slack pass, because those two checks dominate the real system risk.
