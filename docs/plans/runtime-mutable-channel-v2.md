# Plan — runtime-mutable channel parameters, v2

Status: **v2.0 + v2.1 landed** · Builds on: [runtime-mutable-channel.md (v1, landed)](runtime-mutable-channel.md) · Phase target: **Phase 3.x** · Effort actual: **~half day across 5 commits** (v2.0 F1–F4 + v2.1 take_effect_at_slot). v2.2 (warmup contract), v2.3 (multi-link batches) remain. Risk held to **medium**: profile shadow stays a single-writer-per-link POD; eligibility check keeps the use_device_channel dispatch gate static; take_effect_at_slot gating is a single-comparison addition to the snap helper.

## Goal in one sentence

Add three capabilities the v1 control plane deliberately scoped out: full multi-tap profile swaps (replace the entire `taps[]` of a link, not just `taps[0]`), deterministic `take_effect_at_slot` timing so a controller can synchronise updates across slots and across links, and an explicit delay-line warmup contract so the broker logs and the REP carries the slot window during which output samples are warmup artefacts.

## Why v2 now

v1 covered single-link scalar updates. Three use cases now want more:

1. **Profile swaps for handover / mobility experiments** — a UE crosses from a LOS-dominant cell into a heavily-multipath NLOS one. Today only `tap0_*` is mutable; the rest of the multipath profile (`taps[1..N]`, `fading_*`) is YAML-frozen. v2 lets a controller send a full TR 38.901 profile (TDL-A → TDL-C, say) live.
2. **Synchronised multi-link events** — a controller wants both `ue0-gnb0` and `ue1-gnb0` to apply path-loss steps on exactly the same slot (e.g. simulating cell-edge transition where both UEs see correlated fading dips). v1 applies each REQ at the next slot boundary, but multiple REQs over the wire arrive in arrival order, not as a batch.
3. **Warmup-aware analysis pipelines** — when a tap configuration changes the cross-slot `delay_line` ring contents become semantically stale (the ring was filled assuming the old taps). Today the broker silently produces transient output for `dl_size` samples after a tap-delay change. A downstream measurement script can't reliably exclude that window without knowing it occurred.

## What changes and what stays

| Component | After v1 (today) | After v2 |
|---|---|---|
| Mutable shape per link | 7 scalar fields (`MutableParams`, 32 B) | Scalars *plus* an optional full `TapSpec[]` profile (≤ `kDeviceMaxTaps`) + fading sub-config. Total ~1.4 KB/link. |
| `BrokerLinkControl::shadow` | `MutableParams` POD, single-writer-per-link, atomic seqno | **`MutableShadow`** wrapping `MutableParams` + `std::optional<ProfileShadow>` (multi-tap payload). Still single-writer-per-link; seqno still atomic. |
| Message types | `SCALAR_UPDATE` (the only kind; implicit) | `SCALAR_UPDATE` *(unchanged)* + new `PROFILE_SWAP` (whole-taps + fading) + new `BATCH_BEGIN` / `BATCH_COMMIT` for multi-link atomicity. JSON envelope grows a `type` field. |
| `take_effect_at_slot` | Always "next slot boundary"; field ignored if present | Honoured. If unset, semantics fall back to "next slot boundary" (v1 default). Per-link slot counter exposed via `BrokerLinkControl::current_slot`, written by the snap path. |
| `snap_mutable_params()` | Checks seqno; copies shadow → live if advanced | Checks seqno AND `current_slot >= shadow.take_effect_at_slot`. Profile snap path triggers `refresh_all_taps_from_live()` + delay-line zero-fill. |
| Delay-line semantics on tap-change | Silent — ring keeps stale samples; output transients absorb naturally | **Explicit warmup contract**: profile swap zero-fills `delay_line` and `slot_start_samples` window; broker logs `event=control_warmup_begin` / `event=control_warmup_end`; REP body for `PROFILE_SWAP` carries `warmup_until_slot`. Single-tap delay changes treated the same way when the new delay-int differs from the old. |
| CPU↔CUDA parity contract | Bit-exact on deterministic params; statistical on AWGN | *Unchanged for v1 params*. For new tap params: bit-exact *after* the warmup window; **undefined during warmup** (the contract makes the window explicit so downstream tests skip it). |
| Logging | `event=control_start / control_update / control_error` | + `event=control_warmup_begin slot=N link_id=L dl_samples=K` + `event=control_warmup_end slot=N+K link_id=L` + `event=control_batch_begin id=B` + `event=control_batch_commit id=B link_count=C apply_at_slot=N`. |
| Counters in `event=stats` | `control_msgs_received / updates_applied / updates_rejected` | + `control_profile_swaps_applied`, `control_batches_committed`, `control_warmup_slots_total`. |

## Why this version is worth building

Three properties make v2 worth the extra cost vs an incremental "just allow profile swap" patch:

1. **One shadow, one snap point, two payload kinds.** Adding a profile path inside `MutableShadow` keeps the lock-free single-writer-per-link invariant — no new mutex, no separate queue. The snap reads one seqno, branches on whether the shadow carries a pending profile, and routes through the right refresh.
2. **`take_effect_at_slot` and multi-link batches share the same gating.** A `BATCH_COMMIT` is just `N` `SCALAR_UPDATE` / `PROFILE_SWAP` shadows pre-stamped with the same `take_effect_at_slot`. The snap logic doesn't care that they were a batch — it just observes one common apply-slot across links.
3. **Warmup window is the first explicit guarantee the broker makes about its own degraded output.** Today the contract is "every emitted sample is meaningful". v2 introduces a small but typed exception, accompanied by a log line and a REP field. Future v3 features (statistical-only fading swap, ML-driven profile interpolation) reuse the same contract.

The cost is real: ~1.4 KB/link of additional host-side state, ~5 KB extra D2H+H2D per CUDA profile-swap slot, a new slot-counter wire across the broker, and a JSON envelope that grows a `type` discriminator.

## Architectural overview

### Concurrency — same single-writer-per-link, larger payload

```text
control client (ZMQ REQ)            broker
─────────────────────                ───────────────────────────────────
                                     control_server thread
{"type":"profile_swap",                    │  validate full taps[] + fading
 "link_id":"ue0-gnb0",                     │  populate MutableShadow.profile
 "take_effect_at_slot":192833,             │  copy take_effect_at_slot
 "taps":[…],                               │  bump seqno (release-store)
 "fading":{…}}      ──► REP socket ───────┘
                                            │
                                            ▼
                                     per-link MutableShadow
                                     (MutableParams + optional ProfileShadow)
                                     atomic<uint32_t> seqno
                                     atomic<uint64_t> current_slot   ← written by snap path
                                            │
                                            ▼
                                     server thread (one per dst node)
                                     ┌─────────────────────────────────────┐
                                     │ for each slot:                      │
                                     │   for each incoming edge k:         │
                                     │     ctl.current_slot = slot_idx     │
                                     │     uint32_t s = ctl.seqno.load()   │
                                     │     if (s != live_seqno &&          │
                                     │         slot_idx >=                 │
                                     │           shadow.take_effect_at_slot)│
                                     │       if (shadow.profile.has_value())│
                                     │         refresh_all_taps + zero ring│
                                     │         emit control_warmup_begin   │
                                     │       else                          │
                                     │         snap scalars + tap0 refresh │
                                     │       live_seqno = s                │
                                     │   ... H2D / channel kernel / D2H    │
                                     └─────────────────────────────────────┘
```

### Message envelope — grow a `type` field, stay JSON

```json
// SCALAR_UPDATE (v1-compatible, type defaults to "scalar" if omitted)
{"type":"scalar","link_id":"ue0-gnb0","param":"path_loss_db","value":-12.5,"take_effect_at_slot":192833}

// PROFILE_SWAP (new)
{"type":"profile_swap","link_id":"ue0-gnb0","take_effect_at_slot":192833,
 "taps":[
   {"delay_samples":0.0,"gain_db":0.0,"phase_rad":0.0,"is_los":true,"los_k_db":9.0,"los_angle_rad":0.0},
   {"delay_samples":2.5,"gain_db":-3.0,"phase_rad":0.0},
   ...
 ],
 "fading":{"enabled":true,"f_d_max_hz":350.0,"spectrum":"jakes","grid_us":100.0}}

// REP for PROFILE_SWAP carries the warmup window
{"ok":true,"seqno":4711,"applied_at_slot":192833,"warmup_until_slot":192961,"warmup_samples":128}

// BATCH_BEGIN / BATCH_COMMIT (new)
{"type":"batch_begin","id":"exp-42"}   // returns {"ok":true,"batch_id":"exp-42"}
{"type":"scalar","batch_id":"exp-42","link_id":"ue0-gnb0",...}   // up to N messages
{"type":"batch_commit","id":"exp-42","take_effect_at_slot":200000}
// commit returns {"ok":true,"link_count":N,"apply_at_slot":200000}
```

`take_effect_at_slot` precedence: explicit on `BATCH_COMMIT` overrides any per-message value inside the batch. A batch is the atomic unit; partial-batch failure aborts the whole commit and emits `event=control_batch_aborted`.

### Storage layout — sibling fields on BrokerLinkControl, scalars stay POD-fast

The v2 fields are added as **siblings** on `BrokerLinkControl` rather than wrapping `MutableParams` in a new struct. Two reasons: (a) v1's ~30 call sites accessing `ctl.shadow.X` continue to work unchanged; (b) `ProfileShadow` carries its own change-tracking via `profile_pending` so the snap path can branch cleanly.

```cpp
// include/ocudu_gpu_channel/mutable_params.h (unchanged 32-byte POD)
struct MutableParams { /* v1 fields */ };

// include/ocudu_gpu_channel/runtime_control.h (extended)
struct ProfileShadow {
  int       n_taps = 0;
  TapSpec   taps[kDeviceMaxTaps];          // max-size, simplifies lock-free swap
  bool      fading_enabled = false;
  float     fading_f_d_max_hz = 0.0F;
  int       fading_spectrum = 0;           // 0=Jakes
  float     fading_grid_us = 100.0F;
};

struct BrokerLinkControl {
  // v1 scalar shadow (unchanged accessor surface for back-compat).
  MutableParams              shadow;

  // v2 profile-swap shadow. profile_pending is set by the control thread
  // when shadow_profile holds a new profile to apply; cleared by the snap
  // after the refresh runs. Both writes paired with seqno's release-store.
  ProfileShadow              shadow_profile;
  bool                       profile_pending = false;

  // v2 deterministic-timing knob. 0 = "apply at next slot boundary"
  // (v1 semantics, preserved when omitted from the REQ).
  std::uint64_t              take_effect_at_slot = 0;

  // Synchronisation primitives — unchanged from v1.
  std::atomic<std::uint32_t> seqno{0};

  // v2: per-link slot index, written by snap before each gate check; read
  // by control thread to know whether scheduled-in-past updates apply now.
  std::atomic<std::uint64_t> current_slot{0};
};
```

Snap reads scalars + profile in one logical step (single seqno load); since the control thread is the only writer, the data behind a given seqno bump is consistent. ProfileShadow's max-size layout (`TapSpec[kDeviceMaxTaps]`, 32 × 32 B = 1 KB) keeps the snap O(1) memcpy of a fixed-size struct rather than a heap-allocated vector.

### Slot counter — per-link, written by snap path

The broker doesn't currently carry an explicit slot index — it has `slot_start_samples` on each DeviceLinkState that's sample-counted, not slot-counted. v2 adds a per-link `current_slot` counter:

- Incremented by the snap path each time it observes a new slot for that link.
- Read by `snap_mutable_params` to gate `take_effect_at_slot`.
- Read by the warmup-end check to emit `event=control_warmup_end` at the right slot.

Per-link rather than global because the broker's server threads are per-destination-node — there's no global slot tick. Different destinations may advance at slightly different rates.

### Delay-line warmup contract

When a snap applies a profile that changes the tap layout (any tap's `delay_samples` differs from the old, or `n_taps` changes), the per-link `delay_line` ring contents from the old profile are stale and produce incorrect output for the first `dl_size` samples of the next slot batch:

1. Snap zero-fills `host_link_state.delay_line[]` (CPU) or H2Ds zeros over `DeviceLinkState::delay_line[]` (CUDA).
2. Snap sets `host_link_state.warmup_until_slot = current_slot + ceil(dl_size / count)` (typically 1).
3. Broker emits `event=control_warmup_begin slot=N link_id=L dl_samples=K`.
4. When `current_slot >= warmup_until_slot`, broker emits `event=control_warmup_end slot=N+W link_id=L` and clears the flag.
5. The REP body of `PROFILE_SWAP` carries `warmup_until_slot` so the caller can correlate.

Scalar-only updates that don't touch tap layout (the v1 path) skip warmup entirely.

## Phasing

### v2.0 — profile swap with implicit immediate apply (~3 days)

| Step | Deliverable | Verification |
|---|---|---|
| F1 | `ProfileShadow` + `MutableShadow` types in `runtime_control.h`. `BrokerLinkControl::shadow` switches to the wrapping struct; v1 single-writer + seqno semantics unchanged. Existing scalar tests still pass. | Existing 7 ctest cases green. |
| F2 | `ControlServer::handle_message` parses `type` field with default `"scalar"` for back-compat. New branch for `type:"profile_swap"` — validates taps array (count ≤ kDeviceMaxTaps, monotonic delays, gain ranges), validates fading sub-config, writes `shadow.profile`. | New ctest case in `test_control_server`: valid + invalid profile_swap messages. |
| F3 | `refresh_all_taps_from_live()` in `device_channel.cu` — recomputes derived fields for taps[0..n-1] from `shadow.profile`. CPU equivalent in `cpu_backend.cpp` rebuilds effective_taps + effective_polyphase from profile. Delay-line zero-fill helper. | Parity test: profile_swap before slot 1 + bit-exact output post-warmup. |
| F4 | Doc updates: §13 mention multi-tap mutability; §21 'Done' bullet appended. | Anchor health re-run. |

### v2.1 — `take_effect_at_slot` (~2 days)

| Step | Deliverable | Verification |
|---|---|---|
| T1 | Per-link `current_slot` atomic, written at the top of every snap call. | Smoke test: drive 10 slots, assert counter monotonic per link. |
| T2 | `snap_mutable_params` gates: `seqno_advanced && current_slot >= shadow.take_effect_at_slot`. Default behaviour preserved when `take_effect_at_slot == 0`. | Test: write shadow with take_effect_at_slot=N+5, assert live unchanged for 5 slots then applied. |
| T3 | REP body for accepted updates carries `applied_at_slot`. | Test: assert REP echoes the apply slot. |

### v2.2 — delay-line warmup contract (~2 days)

| Step | Deliverable | Verification |
|---|---|---|
| W1 | Tap-layout-change detection at snap time. Zero-fill delay_line, set `warmup_until_slot`. | Test: profile swap with delay change → first slot output is zero-tail-shaped; subsequent slots match the new profile bit-exact. |
| W2 | Emit `event=control_warmup_begin` at snap; `event=control_warmup_end` when `current_slot` reaches the target. New counter `control_warmup_slots_total`. | Recording-logger test: count begin/end events match snap events. |
| W3 | REP body for `profile_swap` carries `warmup_until_slot` + `warmup_samples`. | Test: REP fields present and consistent with the log lines. |

### v2.3 — multi-link batches (~2–3 days)

| Step | Deliverable | Verification |
|---|---|---|
| B1 | `ControlServer` maintains a per-batch staging map keyed by `batch_id`. `batch_begin` allocates, `batch_commit` flushes atomically (per-link seqno bumps in a tight loop with the same `take_effect_at_slot`). `batch_abort` discards. | Test: stage 3 link updates, commit, assert all 3 visible on the same slot. |
| B2 | `event=control_batch_begin` / `event=control_batch_commit` / `event=control_batch_aborted` log lines. New counter `control_batches_committed`. | Recording-logger test counts batch events. |
| B3 | Validation: a stray `scalar` / `profile_swap` with `batch_id` referencing an unopened batch is rejected. | Test case. |

## CPU↔CUDA parity contract

v1's bit-exact-on-deterministic-params contract is preserved for the existing 7 params. Two new contracts in v2:

- **Post-warmup bit-exact parity for profile swaps.** After `warmup_until_slot`, both backends must produce identical IQ output (1e-3) given the same profile. New ctest `test_profile_swap_parity` drives a TDL-A → TDL-C swap on both backends and compares slots `warmup_until_slot..warmup_until_slot+10`.
- **Undefined output during warmup.** Tests deliberately skip the warmup window. The window length is `ceil(dl_size / count)` slots — typically 1 slot at the standard rates. CI documents this in the existing parity-test README.

## Risks and unknowns

- **`std::optional<ProfileShadow>` snap cost.** A ~1 KB structured copy per snap when the optional is set. At 2000 slots/s and 16 links per node that's 32 MB/s of memcpy — comfortable, but per-link cache pressure isn't free. Mitigation: snap copies only when `seqno_advanced`, so steady-state cost stays at one atomic load.
- **Slot-counter consistency under burst load.** If a `take_effect_at_slot` is in the past at the time it's processed, snap applies immediately (the gate `current_slot >= take_effect_at_slot` is satisfied). This is correct but means scheduled-in-past updates lose their timing guarantee. Document; do not over-engineer.
- **Profile swap on a link with no leading tdl.** The CUDA dispatch gate (`use_device_channel`) is set per-destination at `prepare()` based on every incoming edge having a leading tdl. A profile_swap that introduces a leading tdl on a non-tdl-leading edge would in principle let the gate flip on, but flipping mid-run is dangerous (would require re-allocating per-edge `DeviceLinkState` arrays). v2 forbids profile_swap on edges whose YAML had no leading tdl, returns `error="link not eligible for runtime profile swap (configure tdl in YAML)"`.
- **Multi-link batch atomicity vs PCIe gap.** A `BATCH_COMMIT` enqueues N seqno bumps. The server threads consume them on their next snap. If two links sit on different CUDA streams, their `current_slot` ticks can be off by ~1 slot — meaning the batch may not strictly land on the same wall-clock slot. v2 guarantees "same slot per link by index", not "same wall-clock slot". Document.
- **Wire-format growth.** The JSON envelope is fine for human debugging but a 32-tap profile swap is ~3 KB JSON. ZMQ REP can handle it; bandwidth isn't a concern at control-plane rates. Adding a binary fallback is v3.

## Out of scope for v2 (named so they don't drift in)

- Binary message format (v3 — only if telemetry feed pushes back)
- Telemetry PUB feed for closed-loop controllers (v3)
- `noise_power` as a runtime knob (still YAML-only — runtime exposes dB only)
- `phase_rad` on Phase/Cfo steps (still YAML-only — orthogonal to v1's `cfo_hz`)
- Re-evaluating the `use_device_channel` dispatch gate after a swap (forbidden above)
- Per-link auth / ACL on the control socket
- YAML round-trip / `--dump-live-state`

## Resolved design decisions (locked 2026-05-26)

1. **`take_effect_at_slot` default: "next slot boundary".** v2 inherits the v1 semantic when the field is omitted. v1 clients work against a v2 broker without change. Explicit `take_effect_at_slot: 0` is treated the same as omission.
2. **Batch commit slot: same per-link slot index.** All N updates inside a `BATCH_COMMIT` land on the same slot *index* for their respective link. Different links may tick at slightly different wall-clock times because the broker runs one server thread per destination — there's no global tick. Document in the user-facing `event=control_batch_commit` log line; the slot index is what's reported.
3. **Warmup cap: default 3 slots, configurable via CLI.** Reject `profile_swap` if `ceil(dl_size / count) > warmup_cap_slots`. Default cap = 3, override via `--control-warmup-cap-slots N` on the broker. Rejection reply includes the computed warmup span so the caller can either raise the cap or split the swap.
4. **Profile-swap eligibility: allowed with explicit `force=true`.** Default-rejected on edges where `chain.front()` is not a `tdl` step — the new taps would have nothing to apply through. `force=true` overrides the rejection: the server accepts and stores the profile in shadow, the snap copies it to live, refresh updates the derived fields. The CUDA dispatch gate (`use_device_channel`) is *not* re-evaluated; whatever was set at prepare() persists. On a non-tdl-leading edge the profile flows through but doesn't affect kernel output until a YAML reload re-establishes the dispatch. Document that `force=true` is the "I know what I'm doing" path.
