# Control API

The wire contract is implemented in [control_server.cpp](../../src/control_server.cpp), [mutable parameters](../../include/ocudu_gpu_channel/mutable_params.h) and [runtime control state](../../include/ocudu_gpu_channel/runtime_control.h). Control is disabled unless the broker receives `--control-endpoint`; endpoint addresses in examples are explicit choices, not automatic listeners.

## Request, acknowledgment and application

Send UTF-8 JSON on a ZMQ REQ socket and receive one JSON reply before sending the next request. `type` defaults to `scalar` when omitted. `link_id` is the prepared physical edge's `from>to:declared_model` identity; do not invent lane IDs for a matrix update.

For the tutorial topology, a scalar request is:

```json
{"type":"scalar","link_id":"gnb0>ue0:cuda_mvp","param":"cfo_hz","value":125}
```

An illustrative success reply is `{"ok":true,"seqno":2,"applied_at_slot":10}`. Sequence and slot numbers depend on the running broker. This acknowledges the shadow update, not completed IQ processing: the data plane snaps it at a serve boundary. `take_effect_at_slot` defaults to zero (as soon as the consumer reaches it). The ACK's slot is an estimate and can differ from actual application by a slot. Use matching fresh telemetry to observe application.

An unknown message type returns, for example, `{"ok":false,"error":"unknown message type: invalid"}`. Malformed JSON, unknown links/parameters, invalid ranges and unsupported prepared paths return `ok:false` with an error string and emit `control_error`. Do not retry a rejected shape as a different mechanism without changing the requested configuration explicitly.

## Scalar ranges

All scalar values must be finite. `path_loss_db` accepts [-200,50] dB, `awgn_snr_db` [-30,120] dB, `los_k_db` [-30,40] dB, `cfo_hz` [-50000,50000] Hz, `tap0_delay_samples` [0,1023] samples, `tap0_gain_db` [-100,20] dB, and `tap0_phase_rad` [-π,π] radians. Tap-scope updates on `fixed_mimo` are rejected because they would overwrite per-lane coefficients. An accepted scalar only affects a chain that consumes that parameter.

## Batches

Send `{"type":"batch_begin","id":"scene-1"}`, then scalar/profile/matrix requests with `"batch_id":"scene-1"`. Staged updates acknowledge `{"ok":true,"staged":true}` and have not reached live state. Commit with `{"type":"batch_commit","id":"scene-1","take_effect_at_slot":100}`; the response reports `apply_at_slot` and `link_count`. The commit slot overrides individual staged slots. Abort with `{"type":"batch_abort","id":"scene-1"}` to discard staged operations and receive `dropped_ops`.

Atomicity is per-edge slot-index application, not simultaneous wall-clock execution across radios or broker processes. `correlation_swap` does not participate in batch staging. A rejected request does not implicitly abort other valid operations already staged; abort explicitly when abandoning the intended scene update.

(matrix-history)=
## Matrix profiles and history

A `matrix_profile_swap` supplies `nt`, `nr` and exactly `nr * nt` lane objects. Each object has unique `rx_port`, `tx_port` indices and a nonempty `taps` array, with at most 32 taps. Each tap supplies finite delay/gain/phase values; delays are in [0,1023] samples and tap gains in [-100,20] dB. Dimensions must match preparation. Sparse `fixed_mimo` and CUDA host-staging paths reject dynamic matrices.

```json
{"type":"matrix_profile_swap","link_id":"gnb0>ue0:cuda_mvp","nt":1,"nr":1,"lanes":[{"rx_port":0,"tx_port":0,"taps":[{"delay_samples":0,"gain_db":-3,"phase_rad":0}]}]}
```

Identical or gain/phase-only matrix profiles preserve recent input history and existing warmup. First profiles or changed ordered delays/layout reset history and start warmup. The decision applies to the complete physical link. Scalar `profile_swap` retains its reset behavior. This is snapshot replacement, not queued sample-aligned CIR streaming.

Profile ACKs include `warmup_until_slot` as a conservative estimate; observed backend warmup is authoritative. The warmup cap is three slots by default and zero disables the request-time guard. Prepared device history reserves 1031 samples per lane for the 1023-sample bound plus the fractional-filter span.

(section-17)=

(runtime-mutability)=

## Runtime mutability — the control plane

The [architecture](../concepts/architecture.md) describes a broker that reads its YAML once at startup and applies the same chain to every slot. This section is about the second way channel parameters can change: a live ZMQ control plane that mutates per-edge state at slot boundaries without restarting the broker. The data plane stays as described in the [broker loop](../concepts/broker-loop.md) — the control plane meets it once per serve, through a lock-free snap at the top of the slot.

Three reasons this exists rather than relying on a YAML reload. **(1) Latency.** Reload requires tearing down sockets and reallocating device buffers; that's tens of milliseconds and a brief data outage, which is unacceptable during a live UE attach. **(2) Atomicity.** A reload changes every edge at once on whatever slot the broker happens to be serving when the operator restarts; the control plane lets the caller name a `take_effect_at_slot` so a mutation lands at a known point in the UE's experience. **(3) Closed loop.** A controller (RL agent, scripted scenario driver) needs to observe what it just did before its next step; the telemetry PUB feed publishes the post-snap state at a configurable rate so the loop closes without polling REP.

(rtm-architecture)=

### Architecture — two planes that meet at the snap

Every edge gets a `BrokerLinkControl` on the broker side: a `shadow` POD that the control thread writes, a `live` POD that the data plane reads, and a per-edge atomic `seqno` that publishes one to the other. The invariant is **single‑writer‑per‑link**: only the `ControlServer` thread (one per broker process) writes `shadow`; only the per-destination server thread reads it via `snap_mutable_params()` at the very top of every serve. `seqno` is bumped by the writer with release semantics and acquire-loaded by the reader; the snap on a steady-state slot is one acquire-load + early-return.

Control is snapped once per physical link before its antenna lanes are processed. An accepted update and a history reset are separate outcomes: `history_reset_required` governs history clearing and warmup. Coefficient-only matrix updates refresh host/device coefficients while retaining the input-sample history. The shared fractional-filter helper supports numerical comparisons; it does not imply bitwise CPU/CUDA equality. Scalar parameter and scalar `profile_swap` behavior remain unchanged.

(rtm-protocol)=

### Wire protocol — one REP socket, seven message types + telemetry PUB

The control plane is a single ZMQ REP socket bound at `tcp://*:5559` when the broker is launched with `--control-endpoint` (omitted by default; the broker runs with no control plane unless asked). All messages are JSON; the broker replies to every REQ with `{"ok":true, …}` or `{"ok":false,"error":"…"}`. Seven message types cover the surface (telemetry is a PUB frame, not a REQ — see the telemetry paragraph below):

| Message type | Purpose | Key REQ fields | Key REP fields |
|----|----|----|----|
| `scalar` | Mutate one of seven runtime params on one edge (v1). | `link_id, param, value, take_effect_at_slot?, batch_id?` | `applied_at_slot` |
| `profile_swap` | Replace the entire per-edge tap layout at runtime — up to `kDeviceMaxTaps = 32` taps plus an optional fading sub-config (v2.0). | `link_id, taps[], fading?, take_effect_at_slot?, batch_id?, force?` | `applied_at_slot, warmup_until_slot` |
| `matrix_profile_swap` | Replace all antenna-pair tap profiles on a prepared physical link. | `link_id, nt, nr, lanes[{rx_port, tx_port, taps[]}], take_effect_at_slot?, batch_id?` | `applied_at_slot, warmup_until_slot, seqno` |
| `correlation_swap` | Update spatial correlation on an eligible prepared link. | `link_id, kind, rx?, tx?, take_effect_at_slot?` | `seqno` |
| `batch_begin` | Open a caller-keyed staging slot; subsequent `scalar`, `profile_swap` or `matrix_profile_swap` REQs carrying the matching `batch_id` stage instead of apply (v2.3). | `id` | `batch_id` |
| `batch_commit` | Flush every staged op with a single `take_effect_at_slot` shared across every edge touched (per-edge slot-index atomicity). | `id, take_effect_at_slot?` | `apply_at_slot, link_count` |
| `batch_abort` | Discard the staged batch; no shadows touched, data plane unaffected. | `id` | `dropped_ops` |

`correlation_swap` is a direct update; it does not participate in batch staging. Batch begin/commit/abort use `id`; staged scalar/profile/matrix messages reference that value with `batch_id`.

Seven runtime-mutable scalar params (v1): `path_loss_db`, `awgn_snr_db`, `cfo_hz`, `tap0_delay_samples` (float — fractional supported), `tap0_gain_db`, `tap0_phase_rad`, and `los_k_db`. Validation failures (unknown link, unknown param, out-of-range value, malformed JSON) reply with the typed error and emit `event=control_error`; the data plane is unaffected.

**Telemetry PUB feed (v3.0).** A second optional socket bound at `tcp://*:5560` via `--telemetry-endpoint` publishes one frame per edge per `--telemetry-rate-hz` tick (default 20 Hz). Each frame is a JSON `event=telemetry` body with `link_id, slot, seqno, live{…}, profile_active, warmup_until_slot`; the ZMQ topic prefix is `link_id` so a subscriber can filter via `setsockopt(ZMQ_SUBSCRIBE, "ue0>gnb0:cuda_mvp", …)`. Empty subscription receives every edge. PUB high-water-mark loss can be silent. `telemetry_drops` counts only send-side `EAGAIN` results; it cannot establish subscriber delivery or absence of loss. See the [telemetry contract](telemetry.md).

(rtm-events)=

**Events the protocol emits.** The complete event vocabulary is documented in [Event vocabulary](../development/profiling.md#profiling-events); the control-plane subset, in the order it can appear: `control_start`, `control_update`, `control_error`, `control_warmup_begin`, `control_warmup_end`, `control_batch_begin`, `control_batch_commit`, `control_batch_aborted`, `telemetry`, `control_force_warning`, `hardware_probe`, `hardware_footprint`, `hardware_warning`. The new counters this part introduces (`force_inert_warnings`, `batches_committed`, `batches_aborted`, `telemetry_frames`, `telemetry_drops`) are summarised in [Counter taxonomy](../development/profiling.md#counter-taxonomy).

(rtm-warmup)=

### History and warmup

The delay line stores recent **input samples**, which can be reused with new coefficients. For matrix profiles, equal dimensions, tap counts, ordered delays and all non-gain/phase settings preserve history. Delay equality is exact. Identical or gain/phase-only updates update coefficients without clearing, resizing or reinitializing lane history. The decision is shared by every antenna pair on the physical link.

A first matrix profile or a change in delays, tap layout or other settings resets history and starts warmup; different antenna dimensions are rejected. Scalar `profile_swap` retains its existing reset behavior. A preserving matrix update neither restarts nor prematurely ends a warmup already in progress. `control_warmup_begin` and `control_warmup_end` describe actual reset intervals, not every accepted control update.

Scalar and matrix control delays must be finite and within **0–1023 samples**. CUDA preallocates **1031 history samples per lane**, the shared limit plus the eight-sample filter span. Device-state construction and refresh reject insufficient capacity instead of truncating a delay. Warmup duration depends on the required history and batch size; it is not universally one slot.

Control ACKs estimate application/warmup timing. The dashboard uses observed backend status, including a reported `warmup_until_slot=0`, when determining usability. It does not draw an unobserved reset interval. Dynamic matrices on CUDA host staging are rejected before shadow/sequence changes; they require the device-channel route. Static mixed topologies remain supported. Full details and regressions are in [the integration guide](control-api.md#matrix-history).

**`--control-warmup-cap-slots` REQ-time guard.** Profile swaps whose prospective warmup span exceeds the cap (default 3 slots) are rejected at REQ time with a typed error including the computed span, `dl_size`, and slot `count`. The broker publishes per-edge `dl_size_samples_hint` + `slot_count_hint` at prepare time so the check happens without round-tripping through the snap path. `--control-warmup-cap-slots 0` disables the check; warmup span is still emitted via `event=control_warmup_begin`.

(rtm-force)=

### The force flag — visibility on inert chains

A `profile_swap` is rejected by default on edges whose chain doesn't start with `tdl` — the new profile would have nowhere to land. `force: true` in the REQ overrides this: the snap stores and activates the profile, but the per-sample chain has no Tdl branch to read it, so kernel output is unchanged. The broker emits `event=control_force_warning link_id=L reason="chain has no leading tdl; profile stored but inert"` and bumps a per-edge `force_inert_warnings` counter visible in `event=stop`.

This is **Option C** from the v3 plan: explicit visibility without attempting either a silent dispatch-flip (Option B, rejected — surprising to the caller) or runtime chain synthesis (Option A, deferred until a real use case materialises). The data plane stays predictable; the warning is the broker's way of saying "this REQ succeeded but nobody is reading the result."

(rtm-hardware)=

### Hardware probe — the one part that runs without a control endpoint

At broker startup the CUDA backend runs `probe_cuda_hardware(device_id)` and emits `event=hardware_probe ok=true device=N name="…" sm=Mm.M mem_bytes=B driver_version=V runtime_version=V` — a typed report of what CUDA actually sees, separate from compile-time assumptions. CPU-only builds skip the probe; CUDA builds without a visible device emit `ok=false` + a typed error.

Two follow-on checks key off the probe. **Footprint:** `event=hardware_footprint estimated_bytes=B total_mem_bytes=T` reports the device-side memory the topology will allocate; startup rejects with `event=fatal` when the estimate exceeds 80% of `totalGlobalMem` — better to fail at prepare than to OOM mid-serve. **SM (streaming multiprocessor) capability:** if runtime SM is below the kernel's compile-time target (`OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES`, surfaced via `kernel_target_sm()`), the broker emits `event=hardware_warning` and continues so the operator can diagnose; `--hardware-strict` promotes the same condition to `event=fatal`.

(rtm-validation)=

### Validation and dashboard readiness

See [telemetry and dashboard status](telemetry.md) for freshness and readiness, and the [channel validation report](../reports/validation/sionna-merge-fixes-validation.md) for delayed-echo checks. These checks do not establish continuous moving traffic or strict real-time qualification.

Design plans for the three increments live alongside the source: [Runtime mutability v1](../history/designs/runtime-mutable-channel.md) (v1 scalars), [`runtime-mutable-channel-v2.md`](../history/designs/runtime-mutable-channel-v2.md) (v2 profile swap + timing + warmup + batches), [`runtime-mutable-channel-v3.md`](../history/designs/runtime-mutable-channel-v3.md) (v3 telemetry + force-flag + hardware probe). Each plan records its design decisions and implementation boundaries.

(part-v)=
