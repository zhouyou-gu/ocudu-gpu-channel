# Plan — Sionna RT live-channel bridge

Status: proposed architecture, reviewed 2026-08-16; S0 is executable, while S1/S2 require the protocol freeze named below · Scope: external channel-state producer for the existing real-time IQ broker and the approved rank-1 2x1/1x2 then 4x1/1x4 MISO/SIMO path · Reference API: Sionna RT 2.0.1

## Decision

Integrate Sionna RT as an out-of-process **channel-state producer**, not as an IQ processor inside the broker and not as a library linked into the C++/CUDA hot path.

The Sionna sidecar will compute site-specific propagation paths and export bounded channel-impulse-response (CIR) epochs. The proposed broker extension will buffer those epochs ahead of use, map them to the emulator's absolute IQ-sample timeline, and apply them in the existing CUDA channel pipeline. Sionna may run live, generate a look-ahead trajectory, or replay a recorded trace; all three modes use the same wire format.

The first implementation should use Sionna RT's sparse `Paths.cir()` representation. `Paths.taps()` remains an independent offline correctness oracle because it gives the discrete complex baseband response against which the broker's impulse response can be compared. It is not a bit-exact oracle for arbitrary fractional delays: Sionna uses an ideal sinc response over the requested lag range, while the current emulator uses a bounded 8-tap Hamming-windowed sinc interpolator. Tests must therefore declare the bandwidth, sampling frequency, lag range, normalization, and error metric.

## Why this boundary fits the project

Sionna RT 2.0.1 exposes exactly the state the emulator needs:

- `Paths.cir()` returns complex baseband path coefficients and path delays. Its antenna-aware shape is `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths, num_time_steps]` for the coefficients and the same leading antenna/path axes without time for delays.
- `Paths.taps()` converts those paths into discrete complex baseband taps at a caller-selected bandwidth, sampling frequency, lag range, and number of time steps.
- `out_type="numpy"` provides a stable process boundary without making TensorFlow, PyTorch, JAX, Dr.Jit, Mitsuba, or the Python runtime dependencies of the C++ broker.
- Device and scene-object velocities produce path-specific Doppler evolution. For longer motion or path birth/death, Sionna's documented model is to move the scene and retrace.

The local broker already provides the complementary half:

- bounded ZMQ IQ ingress and egress;
- per-edge TDL state and cross-slot delay history;
- slot-boundary control snapshots and deterministic per-link activation;
- multi-link batches that target the same numerical per-link slot (not a global wall-clock or sample-boundary barrier);
- a device-side multi-tap CUDA path; and
- telemetry and strict data-integrity counters.

The missing piece is a buffered, coefficient-bearing external-channel input. The existing `profile_swap` message is not that input: every profile activation treats the tap layout as new, zero-fills the delay line, and enters warmup. Reusing it once per slot would destroy channel continuity.

There is a second current limitation: profile activation refreshes a delay line sized at startup; it does not enlarge that line for a later, longer Sionna path. The S0 compatibility spike may therefore use `profile_swap` only when the startup YAML preallocates a delay window large enough for every exported tap. The exporter must reject an epoch that exceeds that prepared window. A production external-channel ingress must validate this bound directly rather than relying on the current control parser's broader delay range.

## System shape

```text
positions / velocities / scene events
                  |
                  v
        Sionna RT Python sidecar
        PathSolver -> Paths.cir()
                  |
                  | versioned CIR epochs + coefficient chunks
                  | ZeroMQ, binary payload
                  v
        bounded channel-state ingress
        validate -> order -> buffer ahead
                  |
                  | activate by absolute IQ sample index
                  v
        ocudu-gpu-channel CUDA pipeline
        delay history -> directional-vector FIR -> RF impairments -> RX IQ
                  |
                  v
             OCUDU / srsRAN

        telemetry PUB -> producer/controller/recorder
```

Sionna is never on the IQ request/reply critical path. If it pauses, retraces slowly, or crashes, the broker follows an explicit underrun policy instead of blocking OCUDU's ZMQ radio threads.

## Relationship to the MIMO roadmap

The MIMO and Sionna efforts have one merge point, but their early proofs can proceed in parallel:

```text
M0 port-bundle transport -> M1 deterministic 2x1/1x2 vectors --\
                                                               +-> S3 rank-1 CIR vectors -> live OCUDU+srsUE
S0 static 1x1 export -> S1 record/replay -> S2 live 1x1 -------/
```

The authoritative dependency for the approved rank-1 2x1 downlink and 1x2 uplink claim is M0 -> M1 -> S3. S0 and S1 are deliberately scalar so the CIR units, durable format, and replay tooling can be proven without waiting for port transport. S2 may also land on the 1x1 path first, but it is not a substitute for M0/M1. Four gNB ports are a gated extension of the same row/column-vector model, not an end-to-end 4x4 UE claim. The report's delivery cards present the primary antenna-port path; this plan expands the parallel Sionna workstream.

## Two time scales

Sionna's outputs naturally split into two update classes. Keeping them separate is the most important continuity rule in this integration.

### Geometry epoch

A geometry epoch contains the path layout for a directed radio link:

- TX and RX identities and antenna-port ordering;
- path delays;
- path-validity mask;
- carrier frequency, IQ sample rate, and coefficient-grid rate;
- scene, antenna, solver, seed, and normalization metadata; and
- the absolute sample index at which the epoch becomes active.

A new epoch is required after a scene retrace changes path delays, path count, visibility, or antenna mapping. Activating it is allowed to rebuild polyphase coefficients and reset the affected cross-slot delay history under the existing warmup contract.

### Coefficient horizon

A coefficient horizon contains complex `a_k(t)` values for an already-active geometry epoch:

- one or more future coefficient-grid points;
- indexed by RX antenna, TX antenna, path, and time;
- starting at an absolute emulator sample index; and
- sequenced so gaps, duplicates, late frames, and replay are detectable.

Coefficient horizons update complex tap weights only. They must not rebuild tap delays or reset the delay line. The CUDA kernel linearly interpolates between grid points, as the current internal Jakes path already does for its coarse fading grid.

## Canonical Sionna extraction

The sidecar should make normalization and timing choices explicit rather than relying on Sionna defaults:

```python
a, tau = paths.cir(
    sampling_frequency=coefficient_rate_hz,
    num_time_steps=num_grid_points,
    normalize_delays=False,
    out_type="numpy",
)
```

Rules:

1. Use `normalize_delays=False` when physical time of flight is part of the experiment. An optional `relative_delay` mode may subtract the first arrival, but it must be named in metadata because it removes common propagation delay.
2. Preserve Sionna's linear complex coefficients. Do not normalize channel energy unless the experiment explicitly requests it.
3. Convert seconds to sample delay as `delay_samples = tau_seconds * iq_sample_rate_hz` in double precision. Keep fractional delays; the emulator already has an 8-tap fractional-delay filter.
4. Apply `Paths.valid` before export. Invalid or padded paths become absent taps, not NaNs or arbitrary coefficients.
5. Preserve the Sionna axis order in the recorded source data, then map it explicitly to the project's `RadioPortKey {device_id, port}` ordering. Never infer antenna order from dictionary iteration.
6. Emit uplink and downlink as separate directed channel records. The approved srsUE setup is FDD, so solve each direction at its actual carrier frequency with the radio roles and arrays configured in that direction. `reverse_direction=True` only swaps tensor roles; it does not turn a downlink-frequency solve into a validated uplink-frequency channel. Reciprocity may be tested as an explicitly labelled approximation, but must not be assumed implicitly.
7. Treat `a` from `Paths.cir()` as the complex baseband path coefficient. It already contains the propagation phase term at the Sionna carrier and Doppler evolution. The emulator must not add the carrier-delay phase a second time.
8. Make one component the owner of large-scale gain and time variation. When an external Sionna coefficient source is active, do not also apply an internal path-loss or Jakes/Rician coefficient to the same propagation tap unless the experiment explicitly defines and records that cascade.

For a first scalar spike, select one RX antenna and one TX antenna. Do not squeeze antenna axes out of the durable format; the same file and wire schema must extend to the approved 2x1/1x2 and 4x1/1x4 directional channel vectors.

### Direction and port mapping for the approved topology

Build two explicit Sionna configurations for an FDD run:

| Direction | Sionna transmitter | Sionna receiver | Exported shape after path reduction | Broker operation |
|---|---|---|---|---|
| Downlink | OCUDU/gNB array with `N_tx = 2`, later 4 | one-element srsUE array | `[time][1][N_tx][tap]` | one UE output is the coherent sum over gNB TX ports and taps |
| Uplink | one-element srsUE array | OCUDU/gNB array with `N_rx = 2`, later 4 | `[time][N_rx][1][tap]` | one independently modelled output per gNB RX port |

The port manifest is part of the topology hash. For every Sionna antenna index, record the OCUDU endpoint/port id, element position, orientation, polarization, antenna pattern, and direction. Sionna's `num_ant` counts linearly polarized antennas; a dual-polarized physical element can therefore map to two logical antenna indices. Do not equate an element count with an RF-port count without this manifest.

`PathSolver(..., synthetic_array=True)` is the Sionna default. It traces from the array centers and applies element phase shifts with a plane-wave approximation. This is efficient and appropriate only when the aperture is small relative to link and scatterer distances. Record the flag in every epoch. Use `synthetic_array=False` for explicit per-element paths when near-field geometry, element-specific blockage, or array-aperture delay differences are part of the claim. The two modes are different experiments and must not be mixed within one trace.

## Bounded channel representation

The current device channel accepts at most 32 taps and a 128-sample delay-line ring. A raw Sionna scene can exceed both limits. The adapter therefore needs a deterministic reduction policy before a frame is accepted. S0 uses a one-path fixture and performs no clustering or truncation; the general policy becomes load-bearing in S1.

Recommended v1 policy:

1. discard invalid paths;
2. reject non-finite coefficients or delays;
3. reject paths outside the configured delay window;
4. cluster paths whose delays fall within a configurable fractional-sample tolerance;
5. sum clustered paths as complex voltages, not powers;
6. rank remaining paths by integrated energy over the exported horizon;
7. retain at most 32 paths per RX/TX antenna pair; and
8. report retained-energy ratio and dropped-path count in metadata and telemetry.

The adapter must not silently truncate. Experiments that fall below a configured retained-energy threshold fail at export or activation time.

## Wire contract

Use a dedicated channel-state endpoint rather than overloading the JSON REP control socket. JSON remains useful for lifecycle commands and inspection, but a live multi-port coefficient horizon is a multidimensional complex array and should use a fixed binary payload.

The proposed v1 wire record is a ZMQ multipart message:

1. UTF-8 directed port-bundle id;
2. fixed-size little-endian binary header;
3. binary payload; and
4. for `ChannelEpoch` only, UTF-8 JSON provenance metadata.

Complex float values are interleaved IEEE-754 binary32 real/imaginary pairs. The header includes a payload byte count and CRC32C over the header-with-zeroed-CRC plus payload. A trace file stores the same multipart records with a length prefix per part. This custom bounded record avoids a new runtime serialization dependency; schema versioning is mandatory before fields are added.

### `ChannelEpoch` frame

```text
magic                 "OCGCIR01"
schema_version        u16
header_bytes          u16
run_id                u64
epoch_id              u64
topology_hash         u64
frame_sequence        u64
valid_from_sample     u64
iq_sample_rate_hz     u64
carrier_frequency_hz  f64
num_rx_ports          u16
num_tx_ports          u16
num_taps              u16
flags                 u16
coefficient_period_samples  u32
delay_samples[num_rx][num_tx][num_taps]  f64
valid[num_rx][num_tx][num_taps]          u8
```

The epoch also carries or references UTF-8 metadata containing the source version, scene hash, solver options, random seed, antenna-array description, `synthetic_array` setting, delay-normalization mode, and port-name arrays. `num_taps` is the maximum padded tap-slot count across the included antenna pairs; the per-pair `valid` mask disables unused slots. Path reduction and clustering never cross an RX/TX antenna-pair boundary.

### `CoefficientChunk` frame

```text
magic                 "OCGCOEF1"
schema_version        u16
header_bytes          u16
run_id                u64
epoch_id              u64
frame_sequence        u64
first_sample          u64
coefficient_period_samples  u32
num_time_points       u32
num_rx_ports          u16
num_tx_ports          u16
num_taps              u16
flags                 u16
coeff[num_time][num_rx][num_tx][num_taps]  complex-f32
```

`run_id` is generated by the broker for every start and published through telemetry; frames from another run are rejected. The UTF-8 first part is the canonical directed port-bundle id, while `topology_hash` binds the epoch to port ordering and configured topology. The exact hash input is the canonical serialized `(link id, direction, TX port ids, RX port ids, sample rate, carrier frequency)` tuple and must be covered by a golden-vector test.

All integer fields use little-endian order. The receiver validates every dimension multiplication, payload length, CRC, id/hash mapping, and finite floating-point value before allocating or copying.

`first_sample` is authoritative; wall-clock timestamps are diagnostic only. Absolute sample indexing survives the broker's variable-size serves and avoids treating a serve call as a fixed-size slot.

`coefficient_period_samples` must be a positive integer. The sidecar chooses this period first and calls Sionna with `coefficient_rate_hz = iq_sample_rate_hz / coefficient_period_samples`; it never rounds an arbitrary coefficient rate onto the IQ grid.

A chunk's grid points occur at `first_sample + i * coefficient_period_samples`. Its interpolatable coverage ends at its last grid point. Adjacent chunks repeat exactly one endpoint: the next `first_sample` equals the previous last grid-point sample, and the repeated complex vector must match within the wire numeric tolerance. The broker stores the endpoint once and rejects gaps, overlaps beyond that endpoint, or mismatched repeats.

An epoch remains pending until a coefficient chunk covers `valid_from_sample` and the whole epoch is resident. The broker then emits `external_channel_epoch_ready`. Activation occurs only at a processor serve boundary. S2 may initially require `valid_from_sample` to equal such a boundary; support for splitting a serve window at an epoch boundary is a separate extension. A multi-port directional activation additionally waits on the port-bundle barrier, because today's per-link batch control is not a global atomic barrier.

## Transport and buffering

Recommended first transport:

- Sionna sidecar: ZMQ PUSH;
- broker: dedicated ZMQ PULL ingress thread;
- bounded receive high-water mark;
- nonblocking or deadline-bounded send at the producer;
- watermarks and rejection reasons returned through the existing telemetry PUB feed.

The proposed broker extension keeps a configurable look-ahead window. A coefficient chunk is accepted only when:

- its schema version is supported;
- its topology/port mapping matches the configured link;
- its epoch is already buffered for the same `run_id` and directed port-bundle id;
- its sequence and sample range do not conflict with buffered data;
- it is not already late; and
- dimensions and numeric values pass validation.

The proposed ingress thread parses into preallocated or pooled host memory. The CUDA processor will upload a whole future horizon asynchronously before its activation sample and record a CUDA event. The serve path will select only already-resident coefficients; it will not parse JSON, allocate memory, call Python, or wait for an H2D copy whose deadline has not completed.

The timeline owner is a broker-owned monotonic `uint64` sample cursor per logical destination radio bundle. It starts at zero for each `run_id` and advances by the number of IQ samples served. A scalar destination is a one-port bundle. All directional coefficients feeding one destination use the same cursor; MIMO work M0 adds the grouped-port barrier that advances it only after every port is aligned. Reconnects within a run retain the cursor; a broker restart creates a new `run_id` and invalidates queued frames from the old run.

## Underrun and overrun behavior

Failure behavior must be visible and configurable.

Required development policy:

- require `max_hold_samples` in every external-channel topology; `0` means no hold grace;
- `hold_last` for exactly that configured grace window;
- increment `external_channel_underruns` on every affected serve;
- mark the output telemetry as degraded;
- fail strict validation at shutdown if any underrun occurred; and
- stop after the grace window unless an explicit fallback was configured.

Supported fallback modes should be explicit:

- `fail`: stop the broker;
- `hold_last`: freeze the last valid coefficient vector indefinitely (only when explicitly selected as the post-grace fallback);
- `static_profile`: return to the topology's YAML TDL;
- `internal_jakes`: resume the existing statistical fading generator.

Never silently use zeros. A zero channel is a real channel condition and cannot also mean “producer missed its deadline.”

Late and overlapping chunks are rejected with counters. Buffer overflow should backpressure the producer or reject the newest chunk; it must not discard the coefficient chunk currently covering the active sample range.

## CUDA/data-model changes

The external channel will be a new coefficient source for the existing TDL convolution, not a new RF-impairment chain step.

Proposed state split:

```text
DeviceLinkGeometry
  delay_int / frac / polyphase
  validity and port mapping
  cross-slot IQ delay line

DeviceCoefficientHorizon
  epoch_id / first_sample / period
  [time][rx_port][tx_port][tap] complex coefficients
  active and next device buffers

DeviceRfChain
  optional residual calibration gain / CFO / phase
  receiver AWGN applied once after superposition
  internal path loss and Jakes disabled for Sionna-owned propagation taps
```

For 1x1, this replaces the internally generated per-tap Jakes coefficient with an externally supplied complex grid. For the approved downlink, the kernel computes one UE RX output and accumulates over gNB TX port and tap. For uplink, the launch index is `(gNB RX port, sample)` and each output accumulates the one UE TX port over taps. A whole directional-vector horizon activates atomically; individual coefficients cannot switch versions independently.

The internal Jakes/Rician generator remains supported as the zero-dependency default and as a fallback. A topology chooses one coefficient source per directional port bundle:

```yaml
coefficient_source:
  type: sionna_rt
  id: campus_scene
  underrun:
    grace_samples: 115200  # explicit 5 ms at 23.04 MS/s; no implicit default
    after_grace: fail
```

Exact YAML placement should be locked when the directional port-bundle schema is implemented. Avoid embedding endpoint addresses separately in every tap or coefficient.

## Sionna runtime modes

One adapter should support three operating modes.

### Record

Compute a trajectory or sequence of geometry epochs and store an `.ocgcir` trace. This is the first Sionna mode to implement because it gives reproducible test vectors and removes path-solver timing from channel-kernel validation. It can proceed in parallel with the MIMO port-bundle spike.

### Replay

Stream an `.ocgcir` trace according to emulator sample indices. Replay is the deterministic CI and benchmark mode and should work without Sionna installed.

### Live

Accept position, orientation, velocity, or scene-event updates; retrace as needed; generate Doppler coefficient horizons; and maintain the broker's look-ahead watermark. Live mode is considered healthy only when the producer stays ahead for the configured run and reports no missed activation range.

For short motion horizons, use Sionna's Doppler-based `num_time_steps` evolution. Retrace when the validity policy says the fixed-path approximation has expired or when a discrete scene event changes visibility. The threshold is scene- and workload-dependent and must be benchmarked rather than presented as a universal real-time rate.

The coefficient period is an integer number of IQ samples. Choose it by sweeping the maximum exported `Paths.doppler` and measuring phase/interpolation error; do not choose it from a convenient wall-clock rate alone. The required look-ahead is likewise measured:

```text
lookahead_samples >= ceil(iq_rate *
  (p99(retrace + extraction + serialization + transport + H2D) + safety_margin))
```

Static geometry plus Doppler evolution is the first attached-radio live mode. A scene retrace usually changes delays and path identity. The current geometry-activation contract resets the affected history, so repeated retraces can create warmup artefacts that an attached UE observes. Seamless long-motion support therefore requires a later, explicit geometry-transition design: for example, overlapping old/new FIR states with a declared crossfade, or a fixed discrete-delay grid whose coefficients change without resetting history. Until that mechanism is implemented and validated, retracing during traffic is a controlled experiment, not a continuity guarantee.

## GPU placement

Start with process isolation and a host/Numpy wire boundary, even when Sionna and the emulator run on the same machine. This keeps failures and dependencies contained and makes record/replay identical to live operation.

Prefer a separate GPU for Sionna RT when the CUDA-accelerated gNB and channel emulator already share a device. If only one GPU is available, measure path-solver, gNB, H2D, channel-kernel, and D2H timing together before claiming a supported envelope.

CUDA IPC, DLPack, or a shared device buffer can be evaluated later. They are optimizations, not prerequisites, and would couple process lifetime, CUDA contexts, allocation ownership, and Sionna's selected output framework to the broker.

## Phased delivery

### S0 — static export and offline oracle

- Add a Python exporter for a one-path free-space 1x1 fixture at 23.04 MS/s. Place the antennas so time of flight is eight IQ samples; disable non-LOS mechanisms; use physical delay and unnormalized gain.
- Preallocate the startup YAML TDL for at least the fixture's full delay/filter window. Convert the epoch to the existing `profile_swap` JSON once, only to prove delay/gain/phase units; reject it if any tap exceeds the prepared ring.
- Feed an impulse through the CPU reference and compare against `Paths.taps(bandwidth=23.04e6, sampling_frequency=23.04e6, l_min=0, l_max=16, normalize=False, normalize_delays=False)`.

Exit gate: finite values, explicit normalization, delay conversion within `1e-3` sample, retained energy exactly 1.0 for the unpruned one-path fixture, peak-tap location identical, complex peak error no greater than `1e-3` relative to the Sionna peak, and total-energy error no greater than 0.1%. If the ideal-sinc and bounded-filter tails prevent this gate despite the integer-delay fixture, record the evidence and compare the broker against an independently evaluated bounded-filter reference rather than widening the tolerance silently.

### S1 — durable record/replay format

- Freeze the v1 multipart header layout, CRC coverage, topology-hash golden vector, link-id rules, chunk endpoint rule, and trace length prefixes described above.
- Implement `ChannelEpoch` and `CoefficientChunk` serialization.
- Add trace inspection and deterministic replay tools that do not import Sionna.
- Add corruption, dimension, NaN, sequence-gap, and topology-hash tests.

Exit gate: a recorded trace replays bit-for-bit at the wire level and produces repeatable CPU channel output.

### S2 — buffered live 1x1 coefficients

- Add the dedicated ingress endpoint, host ring, sample-index activation, and underrun policy.
- Split geometry activation from coefficient-only activation.
- Add an external-coefficient grid to the CPU and CUDA TDL paths without resetting IQ delay history.

Exit gate: a constant-Doppler one-path scene remains phase-continuous across chunks; CPU and CUDA agree; deliberate late/missing frames trigger the specified counters and fallback.

### S3 — rank-1 2x1/1x2 directional channels

- Land the port-bundle transport and directional-vector TDL data model from the MIMO plan.
- Preserve Sionna's RX/TX antenna axes in the epoch and kernel.
- Activate both coefficients of the downlink row or uplink column at one sample boundary; later repeat the same gate with four gNB ports.

Exit gate: downlink branch isolation/coherent sum and uplink branch isolation match the CPU oracle for Sionna-exported 2x1/1x2 traces and keep all gNB ports sample-aligned.

### S4 — live mobility and OCUDU validation

- Feed scene position/velocity updates into the Sionna sidecar.
- Maintain a measured look-ahead horizon while periodically retracing.
- Run synthetic radio peers first, then the existing 1x1 srsUE attach regression, then the approved 2x1 downlink and 1x2 uplink srsUE run. Gate 4x1/1x4 only after the two-port result passes.

Exit gate: the producer stays ahead, no channel frames are late or missing, broker data-integrity counters remain clean, and claims are limited to the tested topology/hardware/duration.

## Validation matrix

| Surface | Test | Required result |
|---|---|---|
| Units | one line-of-sight path | delay, magnitude, and phase agree with the exported CIR |
| Normalization | `normalize_delays` and energy modes | metadata and output distinguish physical from normalized channels |
| FIR | impulse through recorded epoch | integer-delay peak agrees; fractional-delay response meets declared time/frequency/energy tolerances against `Paths.taps()` and the broker's bounded-filter reference |
| Time evolution | known single-path Doppler | phase increment agrees over chunk and epoch boundaries |
| Continuity | coefficient-only update | delay line is preserved; no warmup event |
| Geometry change | delay/path-layout change | declared reset and warmup occur at the activation sample |
| Ordering | duplicate, gap, late, and overlapping chunks | deterministic reject/accept behavior and counters |
| Buffering | producer stall | declared hold/fail/fallback behavior; IQ thread never waits on Sionna |
| Directional vectors | 2x1 DL coherent/cancelling cases and 1x2 UL branch isolation | expected complex sum at UE RX0 and expected independent vectors at gNB RX0/RX1 |
| FDD | separate DL/UL carrier solves | direction, frequency, role, port manifest, and topology hash are correct; no implicit reciprocity |
| Array model | synthetic versus explicit array fixture | port order is identical and approximation error is measured for the chosen scene |
| Backend | CPU versus CUDA | established numeric tolerance maintained |
| System | recorded then live Sionna trace | clean ZMQ/data-integrity counters and measured producer lead |

## Telemetry additions

Publish at least:

- `external_channel_epoch_id`;
- `external_channel_frame_sequence`;
- `external_channel_active_sample`;
- `external_channel_buffered_until_sample`;
- `external_channel_lead_samples`;
- `external_channel_retained_energy_ratio`;
- `external_channel_frames_accepted`;
- `external_channel_frames_rejected`;
- `external_channel_late_frames`;
- `external_channel_sequence_gaps`;
- `external_channel_underruns`; and
- `external_channel_fallback_activations`.

The Sionna sidecar should subscribe to the existing telemetry PUB feed and pace its look-ahead production from the broker's accepted watermark, not from wall-clock guesses.

## Explicit non-goals for the first bridge

- running Python or Sionna inside the broker process;
- ray tracing synchronously once per IQ slot;
- replacing the existing internal TDL/Jakes models;
- zero-copy CUDA sharing before the portable wire contract works;
- inferring antenna ordering or reciprocity;
- claiming full CDL/beamforming fidelity merely because Sionna supplied a multi-port tensor; or
- treating an opened second ZMQ antenna port as proof of rank-greater-than-one NR behavior.

## Open decisions before S2/S4

1. The default coefficient-grid period and look-ahead target. These must be chosen from Doppler-error and throughput measurements, not guessed into a public performance claim.
2. The exact path-clustering tolerance and retained-energy threshold for scenes with more than 32 paths.
3. Whether geometry epochs may change the common propagation delay during an attached live-radio run, or whether common delay is held fixed and only excess delay is updated.
4. The bounded ingress capacity and newest-frame rejection/backpressure threshold after the active range is protected.
5. The live-mobility input protocol, coordinate frame, time base, and retrace-validity rule. These are S4 decisions and do not block record/replay or externally scripted coefficient horizons.
6. Whether the validated scene permits synthetic arrays or requires explicit per-element tracing, and the maximum acceptable approximation error.
7. The geometry-epoch continuity mechanism for retraces during attached traffic: declared warmup, fixed tap grid, or dual-state transition/crossfade.
8. The exact DL/UL FDD scene construction and material-frequency policy; `reverse_direction` is not an implicit answer.

## Primary references

- [Sionna RT 2.0.1 `Paths` API](https://nvlabs.github.io/sionna/rt/api/paths.html) — CIR, discrete taps, antenna/path axes, Doppler, normalization, sampling, and Numpy export.
- [Sionna RT introduction](https://nvlabs.github.io/sionna/rt/tutorials/Introduction.html) — worked `Paths.cir()`, `Paths.taps()`, and time-evolution examples.
- [Sionna RT mobility tutorial](https://nvlabs.github.io/sionna/rt/tutorials/Mobility.html) — Doppler-based short-horizon evolution versus moving and retracing scene geometry.
- [Sionna RT path-solver API](https://nvlabs.github.io/sionna/rt/api/paths_solvers.html) — synthetic-array default and explicit-versus-synthetic array behavior.
- [Sionna RT technical report, path solver](https://nvlabs.github.io/sionna/rt/tech-report/S3.html) — synthetic-array plane-wave approximation and its aperture/distance validity boundary.
- [Sionna RT repository](https://github.com/NVlabs/sionna-rt) — standalone package, installation boundary, version, and Apache-2.0 license.
- [Local runtime control plan](runtime-mutable-channel-v2.md) — deterministic per-link activation, profile warmup, and batches targeting one numerical per-link slot.
- [Local MIMO assessment](../mimo-integration-report.html) — approved rank-1 directional antenna-port and vector-TDL extension.
