# Plan — runtime-mutable channel parameters

Status: **v1 fully landed (C1–C6 + v1-fin-A/B/C)** · Phase target: **Phase 3** · v1 effort actual: **~1 focused day across 10 commits** · Risk: held to **low** (every commit kept tests green; CPU↔CUDA parity preserved on deterministic params, statistical on AWGN). All seven v1 params (`path_loss_db`, `awgn_snr_db`, `cfo_hz`, `tap0_delay_samples` (float), `tap0_gain_db`, `tap0_phase_rad`, `los_k_db`) flow end-to-end through the control plane and into kernel output. v2 (multi-tap profile swaps + `take_effect_at_slot`) is the next increment.

## Goal in one sentence

Add a ZMQ control plane that lets external clients update per-link channel parameters (path-loss, AWGN, K-factor, CFO, single-tap delay/weight) atomically at slot boundaries, without restarting the broker, while keeping the CPU↔CUDA parity guarantee on every applied update.

## Why now

The doc has flagged this for a while ([§21 *Planned — operational flexibility*](../index.html#scope)). Three concrete use cases push it to the front of the queue:

1. **Closed-loop link-adaptation experiments** — a controller varies SNR over time and observes how an MCS-selection algorithm reacts. Today this needs broker restart per SNR point and stitching the resulting traces, which destroys cross-experiment timing.
2. **Handover triggers** — step path-loss to a different value at slot *T* so a UE re-evaluates serving cell. Static YAML can't express the step.
3. **Time-varying Doppler beyond stationary Jakes** — non-stationary mobility scenarios. Today the channel statistics are fixed for the run.

This plan is for v1 (scalar params). Multi-tap profile swaps and a streaming telemetry feedback loop are future increments scoped at the end.

## What changes and what stays

| Component | Today | After v1 |
|---|---|---|
| YAML topology + models | Loaded once at startup, frozen until restart ([broker.cpp:420-435](../../src/broker.cpp#L420-L435)) | **Initial state only** — loaded once at startup *(unchanged)*. Runtime mutations override initial state per-link. |
| `DeviceLinkState` mutability | `delay_line[]` + `slot_start_samples` only ([device_channel.h](../../include/ocudu_gpu_channel/device_channel.h)) | Adds **`MutableParams`** sub-struct: `path_loss_db`, `awgn_sigma`, `los_k_db`, `cfo_hz`, single-tap `delay_samples` / `gain_db` / `phase_rad`. All other fields stay immutable. |
| Control plane | none | **New** `control_server` thread + ZMQ REP socket on a configurable port (default `tcp://*:5559`). Message format: framed JSON `{link_id, param, value, take_effect_at_slot?}`. |
| Update synchronisation | n/a | **Per-link double-buffered params**: control thread writes to *shadow*; server thread snaps `shadow → live` at the start of each serve, before the H2D. Lock-free (single writer per link + atomic seqno). |
| `LinkModelState` (host fallback) | Mirrors `DeviceLinkState` once at `prepare()` ([cuda_backend.cu:60-78](../../src/cuda_backend.cu#L60-L78)) | Same shadow/live double-buffer mechanism, applied symmetrically. |
| CPU↔CUDA parity tests | Bit-exact (scalar steps) + statistical (fading) at startup ([test_processing.cpp](../../tests/test_processing.cpp)) | **+ new test**: drive a sequence of runtime updates against both backends, assert parity tolerance on every post-update slot. |
| Logging | `event=process` per slot, `event=gpu_timings`, `event=stats` ([§19 of the reference](../index.html#profiling)) | **+ `event=control_update`** per applied mutation: `link_id`, `param`, `old`, `new`, `applied_at_slot`, `seqno`. **+ `event=control_error`** for rejected messages. **+ counters** `control_updates_applied / control_updates_rejected / control_msgs_received` in `event=stats`. |
| Strict-realtime mode | Existing data-integrity counters | Reject any update message that arrives while the broker is past the slot deadline; counter `control_updates_dropped_realtime`. |
| Broker CLI | `--config`, `--duration`, `--strict-realtime`, … | `+ --control-endpoint tcp://*:5559` (default off — opt-in feature). |

## Why this is the version worth building

Three properties make v1 worth shipping ahead of multi-tap profile swaps and telemetry feedback:

1. **Single-writer-per-link concurrency** is straightforward to reason about and verify. The control thread is the only writer to each link's shadow buffer; the server thread is the only reader from live. A single relaxed-load atomic seqno carries the snapshot signal; no locks on the hot path.
2. **Scalar parameters don't break cross-slot history.** `path_loss_db`, `awgn_sigma`, `cfo_hz` apply per-sample with no memory of prior slots. `los_k_db` shifts the Rician balance but the underlying Jakes coefficients are recomputed each slot anyway. Multi-tap `delay_samples` changes do invalidate the `delay_line` ring, which is why they live in v2 with an explicit warmup contract.
3. **Bit-exact CPU↔CUDA parity survives.** Every v1 parameter is a scalar that both backends apply identically; the existing parity test extends naturally — drive the same update sequence into both backends, compare outputs slot-by-slot. No tolerance widening.

The cost: about 250–400 LOC across the new control server, the shadow/live mechanism, the CLI flag, and tests; one new ZMQ socket per broker process; documentation updates in three places.

## Architectural overview

### Concurrency — single-writer-per-link, snap-at-slot-boundary

```text
control client (ZMQ REQ)              broker
─────────────────────                  ──────────────────────────────────────
                                       control_server thread (single writer)
{link_id:"ue0-gnb0",                          │
 param:"path_loss_db",                        │  validate + look up link
 value:-12.5}    ──────► REP socket  ─────────┘
                                              │
                                              ▼
                                       per-link shadow buffer
                                       MutableParams shadow[N_LINKS]
                                       atomic<uint32_t> seqno[N_LINKS]
                                              │
                                              │  (control thread writes shadow,
                                              │   bumps seqno atomically)
                                              │
                                              ▼
                                       server thread (one per dst node)
                                       ┌─────────────────────────────────┐
                                       │ for each slot:                  │
                                       │   for each incoming edge k:     │
                                       │     uint32_t s = seqno[k].load()│
                                       │     if (s != live_seqno[k]) {   │
                                       │       memcpy live ← shadow      │
                                       │       live_seqno[k] = s         │
                                       │       log event=control_update  │
                                       │     }                           │
                                       │   H2D (DeviceLinkState w/ live) │
                                       │   apply_channel_kernel          │
                                       │   superpose_kernel              │
                                       │   D2H, REP send                 │
                                       └─────────────────────────────────┘
```

The seqno is the per-link snapshot-version counter. A control update bumps shadow then bumps seqno; the server's `load()` is a `memory_order_acquire` paired with the writer's `memory_order_release` so the shadow write is observed before the seqno. No locks. The memcpy of `MutableParams` (a small POD, ~64 bytes) is the snap-at-slot-boundary operation.

### Message format — minimal JSON, validated against per-param schema

```json
// REQ
{"link_id": "ue0-gnb0", "param": "path_loss_db", "value": -12.5}

// REP — success
{"ok": true, "seqno": 4711, "applied_at_slot": 192833}

// REP — validation failure
{"ok": false, "error": "param 'path_loss_db' out of range [-200, 50]"}
```

Param whitelist for v1 (rejected if unknown):

- `path_loss_db` — float, range `[-200, 50]`
- `awgn_sigma`  — float, range `[0, 10]` (linear amplitude)
- `los_k_db`    — float, range `[-30, 40]`
- `cfo_hz`      — float, range `[-50_000, 50_000]`
- `tap0_delay_samples` — int, range `[0, 1023]` *(single-tap only — v1 forbids on multi-tap links)*
- `tap0_gain_db`       — float, range `[-100, 20]`
- `tap0_phase_rad`     — float, range `[-π, π]`

`take_effect_at_slot` is **deferred to v2** — v1 applies "at next slot boundary, whichever comes first". Deterministic timing across multiple links is a future-increment problem.

### Param storage layout — separate mutable from immutable

```cpp
// include/ocudu_gpu_channel/device_channel.h
struct MutableParams {           // 64 bytes, POD, trivially copyable
  float    path_loss_db;
  float    awgn_sigma;
  float    los_k_db;
  float    cfo_hz;
  int32_t  tap0_delay_samples;   // forbidden if n_taps > 1 in v1
  float    tap0_gain_db;
  float    tap0_phase_rad;
  uint32_t _pad;                 // align to 32 bytes for clean copy
};

struct DeviceLinkState {
  // ── Immutable (built at prepare(), never written after) ──
  int32_t     src_index;
  int32_t     n_taps;
  TapSpec     taps[K_MAX];          // for n_taps > 1, taps[1..] stay immutable in v1
  float       polyphase[K_MAX][8];
  // … (existing immutable fields)

  // ── Mutable (snapped at slot boundary) ──
  MutableParams live;               // what the kernel reads this slot

  // ── Existing mutable (already written back by kernel) ──
  cuComplex   delay_line[DL_SIZE];
  int64_t     slot_start_samples;
};
```

On the host side, a parallel `LinkModelState` carries the same `MutableParams live` for the host fallback path. Both backends read from `live`; both ignore `shadow` (which lives in `BrokerLinkControl`, host-only).

### Control thread + per-link shadow

```cpp
// src/control_server.cpp (new)
struct BrokerLinkControl {
  MutableParams        shadow;       // written by control thread only
  std::atomic<uint32_t> seqno{0};    // bumped after every shadow write
};

class ControlServer {
public:
  ControlServer(zmq::context_t& ctx,
                const std::string& endpoint,
                std::unordered_map<std::string, BrokerLinkControl*> link_map);
  void run();   // blocks on REP socket
  void stop();
private:
  zmq::socket_t  socket_;
  std::unordered_map<std::string, BrokerLinkControl*> link_map_;
  std::atomic<bool> stop_requested_{false};
};
```

`link_map` is built once at `prepare()` from YAML link IDs. Lookup is O(1). The server validates against the param whitelist, applies the update to `shadow`, then bumps seqno with `memory_order_release`.

### Server-thread snap (single new function in the hot path)

```cpp
// src/cuda_backend.cu — new helper called once per edge per slot, before H2D
__host__ inline void snap_mutable_params(CudaLinkSlot& slot,
                                         BrokerLinkControl& ctl,
                                         /* logger */) {
  uint32_t s = ctl.seqno.load(std::memory_order_acquire);
  if (s == slot.live_seqno) return;
  slot.host_link_state.live = ctl.shadow;   // POD memcpy, 64 B
  slot.live_seqno = s;
  log_event_control_update(slot, ctl.shadow, s);
}
```

The H2D that already ships `DeviceLinkState` to `device_link_state[]` carries the updated `live` field naturally — no extra copies. For all-immutable slots (no pending update), the load is a single atomic with the cache line warm from last slot; cost is negligible.

### Logging — every applied update is reconstructable from logs

New event lines, all on the same stdout sink the existing instrumentation uses ([§19 of the reference](../index.html#profiling)):

```json
{"event":"control_update","ts":"...","link_id":"ue0-gnb0","param":"path_loss_db","old":-10.0,"new":-12.5,"applied_at_slot":192833,"seqno":4711}
{"event":"control_error","ts":"...","link_id":"unknown","error":"link_id not found"}
{"event":"stats","ts":"...","control_msgs_received":4712,"control_updates_applied":4711,"control_updates_rejected":1,"control_updates_dropped_realtime":0,...}
```

This makes every experiment reproducible from `event=control_update` traces alone.

## Phasing

### v1 (this plan) — scalar params, ~5–7 days

| Step | Deliverable | Verification |
|---|---|---|
| C1 | `MutableParams` struct + `live` field in `DeviceLinkState` / `LinkModelState`. Both backends read from `live` instead of immutable fields for the seven v1 params. Initial value mirrors YAML. | Existing tests still pass (no behavioural change — `live` initialised to YAML). |
| C2 | `BrokerLinkControl` shadow + atomic seqno; `snap_mutable_params()` helper; integration into the server thread's per-slot loop. | Unit test: drive shadow writes from a fake control source, assert server picks them up at slot boundaries. |
| C3 | `ControlServer` ZMQ REP class + param whitelist validation + `--control-endpoint` CLI flag. | Unit test: send valid + invalid messages over `tcp://127.0.0.1`, assert REP responses match schema. |
| C4 | Log events `control_update` + `control_error` + new `stats` counters. | Smoke: drive 100 updates, grep logs, count must match. |
| C5 | CPU↔CUDA runtime-update parity test: scripted update sequence (`{path_loss_db: -10 → -20 → -5}`), assert backends agree slot-by-slot. | New ctest case `test_runtime_update_parity`. |
| C6 | Doc updates: §13 (RF impairments — mention the mutable contract), §19 (logging — new events), §21 (move "Runtime-mutable" out of *Planned* into *Done* with the parity-test reference). | Visual QA + anchor health re-run. |

### v2 (separate plan) — multi-tap profile swaps + deterministic timing

- `take_effect_at_slot` becomes load-bearing — caller specifies *when*.
- Multi-tap profile swap requires a **`delay_line` warmup contract**: the broker zero-fills the ring for the new profile, the caller accepts that the next `dl_size` samples are warm-up artefacts.
- New param `profile_swap` carrying a full `TapSpec[]` payload.
- Statistical parity (not bit-exact) for the warmup window, then bit-exact again.

### v3 (separate plan) — telemetry feedback

- Broker exposes a **`tcp://*:5560` PUB socket** streaming per-slot per-link telemetry (instantaneous SNR estimate, power, recent counter deltas).
- Lets a controller close the loop without polling REQ/REP.
- Bandwidth budget: 16 links × ~80 B/slot × 2000 slots/s ≈ 2.5 Mbps — comfortable.

## Bit-exact parity contract

The project invariant ([CLAUDE.md / AGENT_HARNESS.md](../../AGENT_HARNESS.md)) is **CPU↔CUDA bit-exact at 1e-3 tolerance** for all scalar steps, statistical parity for fading. v1 preserves this:

- Every v1 param flows through the same `apply_chain()` device function and the same host `apply_step()` helper. The shadow → live snap is host-side, before the device function runs. Both backends consume an identical `live` snapshot for the same slot.
- New ctest `test_runtime_update_parity` runs a 100-slot scripted update sequence on both backends in lockstep, asserts max abs deviation < 1e-3 per IQ sample (same threshold as today's chain-step parity).
- For the fading params (`los_k_db`), parity is statistical at the same tolerance the existing Jakes test uses — empirical K-factor recovery within ±0.5 dB over a window.

## Risks and unknowns

- **Multi-link atomicity.** v1 does not provide "apply these 5 updates together at slot T" semantics. A controller that wants synchronised mutations of multiple links can fire all 5 REQs within one slot's wall-clock budget (~500 µs at 30 kHz SCS), but they will be observed in arrival order, not as a batch. v2's `take_effect_at_slot` solves this; if synchronised multi-link is needed sooner, this plan needs a small extension (a `commit` REQ that flips a batch of pre-staged updates atomically).
- **Control-thread starvation under load.** A flood of REQs could starve the broker's ZMQ thread pool. Mitigation: the control socket runs on its own dedicated thread with its own context, no shared sockets. Worst case is the control plane becomes unresponsive; the data plane is not affected.
- **Seqno wraparound.** `uint32_t` rolls over at ~4.3B updates. At 2000 slots/s that's 25 days of one-update-per-slot — fine for any realistic experiment. Document as a known limit; v3 might widen if needed.
- **YAML round-tripping.** Out of scope for v1. The YAML is the initial state; live state is in the broker's memory and logs. A future `--dump-live-state PATH` flag could serialise current `live` back to YAML, but no use case demands it yet.
- **Security.** v1 ships an unauthenticated ZMQ REP socket. Same trust model as the existing data-plane ZMQ. If the broker is reachable from untrusted networks, the operator already has to firewall the data ports; the control port is the same story. Document as the same trust boundary; do not add auth in v1.
- **Param-set growth pressure.** Once v1 ships, every "can you also make X mutable" ask will be tempting. The whitelist is the gate — adding a param requires a parity test + a doc update. Resist drive-by additions.

## Out of scope for v1 (named so they don't drift in)

- Multi-tap profile swaps (→ v2)
- Per-link snapshots with `take_effect_at_slot` (→ v2)
- Telemetry PUB feed (→ v3)
- Auth on the control socket
- YAML round-trip / state dump
- Per-link policy ("step over N slots") — caller implements that by sending N updates

## Files this plan will touch

```text
include/ocudu_gpu_channel/
  device_channel.h          — add MutableParams + live field
  control_server.h          — new
src/
  device_channel.cu         — read MutableParams in apply_channel_kernel
  cuda_backend.cu           — add snap_mutable_params() in server hot path,
                              build link_map at prepare()
  broker.cpp                — instantiate ControlServer when --control-endpoint set
  control_server.cpp        — new
tests/
  test_runtime_update_parity.cpp  — new (C5 above)
  test_control_server.cpp         — new (C3 above)
docs/
  index.html                — §13 + §19 + §21 updates per C6 above
  plans/runtime-mutable-channel.md  — this file, move to in-progress when started
examples/
  control-update-cli.py     — new, tiny REQ client for smoke testing
```

## Resolved design decisions (locked 2026-05-26)

1. **Default port: `5559`.** Sits one above the conventional ZMQ data-plane range (srsRAN/OCUDU use 2000s) and avoids the FlexRIC ports (5557/5558). Wire as `tcp://*:5559` behind the opt-in `--control-endpoint` flag.
2. **Message format: JSON.** Human-readable, debuggable from a shell, no new dependencies. Modest parsing overhead is fine — the control plane is low-bandwidth. Revisit only if v3 telemetry throughput pushes back.
3. **Static-step vs mutable-param conflict: mutable overrides static, warn at `prepare()`.** When a link has both a static `path_loss` chain step and lives in the control map, the mutable value wins from slot 0 onward; `prepare()` emits a `event=control_warning` log line naming the link and both sources so the override is never silent.
4. **CI cadence: every push for the first month, then nightly.** The slot-by-slot parity test runs on every push while the code is fresh, then moves to a nightly job after 30 days of stable green. Track the cutover date in `AGENT_PROGRESS.md` when v1 lands.
