# Plan — Phase 3 v3: telemetry feed, force-flag effects, hardware checks

Status: **v3 fully landed (v3.0 + v3.1 + v3.2)** · Builds on: [v1 plan](runtime-mutable-channel.md) (landed) + [v2 plan](runtime-mutable-channel-v2.md) (landed) · Phase target: **Phase 3.x** · Effort actual: **~3 focused commits across three sub-phases**.

## Goal in one sentence

Close the control-plane loop with a PUB telemetry feed so external controllers don't poll, give the v2.0 `force=true` flag an actual data-plane effect on non-tdl-leading chains, and add startup-time hardware probes that validate the GPU is the one the topology assumes.

## Three heterogeneous areas, three sub-phases

The three v3 items are deliberately independent. They can land in any order; this plan sketches them as **v3.0 → v3.1 → v3.2** because telemetry is the smallest standalone surface and a useful debugging tool for the other two.

| Area | Scope | Risk | Touches data plane? |
|---|---|---|---|
| v3.0 Telemetry PUB feed | New ZMQ PUB socket; per-link per-slot telemetry; opt-in CLI flag | Low (additive) | No (read-only from snap) |
| v3.1 Force-flag effects | Make `force=true` on profile_swap actually flip a link's dispatch path | Medium-high (dispatch gate was static-by-design) | Yes |
| v3.2 Hardware checks | GPU presence, compute capability, memory headroom probes at broker startup | Low | No (startup-only) |

## v3.0 — Telemetry PUB feed

### Use case

Closed-loop experiments today need to either poll the REP socket (latency-bouncy, scales poorly past 10–20 links) or tail the broker's stdout logs (parse-fragile, no flow control). A dedicated PUB feed lets a controller subscribe to per-link state changes and react inside one slot's wall-clock budget.

### What changes and what stays

| Component | Today | After v3.0 |
|---|---|---|
| ControlServer transports | REP only (`tcp://*:5559`) | REP + optional PUB (`tcp://*:5560`) |
| CLI flags | `--control-endpoint` | + `--telemetry-endpoint tcp://*:5560` + `--telemetry-rate-hz 20` |
| Snap path | Updates `ctl.current_slot` + applies live + (v2.2) zero-fills delay_line | + atomically updates a small `TelemetrySnapshot` per link (no allocation; flat POD) |
| Telemetry thread | n/a | New thread on the control plane's ZMQ context; wakes on `1 / telemetry_rate_hz` interval; iterates link_map and PUBs a JSON frame per link |
| Stats counters | msgs_received / updates_applied / updates_rejected / batches_* | + telemetry_frames_sent, telemetry_drops (PUB high-water mark hit) |

### Message envelope — one PUB frame per link per tick

```json
{
  "event": "telemetry",
  "link_id": "ue0-gnb0",
  "slot": 192833,
  "seqno": 4711,
  "live": {
    "path_loss_db": -12.5,
    "awgn_snr_db": 25.0,
    "cfo_hz": 0.0,
    "tap0_delay_samples": 0.0,
    "tap0_gain_db": 0.0,
    "tap0_phase_rad": 0.0,
    "los_k_db": 9.0
  },
  "warmup_until_slot": 0,
  "profile_active": false
}
```

ZMQ topic prefix is the `link_id` so subscribers can filter — `socket.setsockopt(ZMQ_SUBSCRIBE, "ue0-gnb0", …)` receives only that link's frames. An empty subscription receives all links.

### Concurrency

- Telemetry thread is the only reader of `TelemetrySnapshot`; snap path is the only writer. Both use relaxed atomics on a per-link `telemetry_seqno` so the telemetry thread can detect torn reads and re-fetch.
- PUB socket has `ZMQ_SNDHWM = 1000` (default); if a subscriber is slow, frames drop silently. `telemetry_drops` counter (returned by `zmq_send`'s EAGAIN) makes drop visibility explicit.
- The thread sleeps `1 / telemetry_rate_hz` between ticks. No coupling with snap timing — frames are samples, not events.

### Phasing

| Step | Deliverable | Verification |
|---|---|---|
| TM1 | `TelemetrySnapshot` POD + per-link field on backend state. Snap path writes the snapshot at the end of every call. | Existing 7 ctest cases still pass. |
| TM2 | `ControlServer` gains optional PUB socket + telemetry thread on top of the existing REP loop. `--telemetry-endpoint` and `--telemetry-rate-hz` flags. | Unit test: bring up PUB, subscribe with a ZMQ_SUB on the same port, drive one snap, assert the SUB receives the expected JSON. |
| TM3 | Topic prefix per link + `telemetry_frames_sent` / `telemetry_drops` counters in `event=stats`. | Unit test: subscribe with prefix, drive snaps on 2 links, assert only the subscribed link's frames arrive. |

## v3.1 — Force-flag effects

### Today's reality (recap)

v2.0 added `force: true` on profile_swap REQs as the override for the "chain has no leading tdl" eligibility check. The snap accepts the profile and copies it into `live_profile`, but the data plane is unchanged: the YAML chain still has no leading tdl, so `chain_has_leading_tdl == false` and the kernel's per-tap convolution path is never invoked for that edge's contribution. The forced profile sits in `live_profile` as a no-op.

### The decision the user needs to make

There are three plausible meanings for "force should have an effect":

| Option | Behaviour | Cost | Footgun risk |
|---|---|---|---|
| **A. Kernel-chain wrapping** | Snap synthesises a `tdl` step from the live profile and inserts it as `chain.front()` for that edge. Subsequent per-sample chain runs through the new tdl + the YAML chain unchanged. | Adds a per-edge "synthesised chain head" — backend has to merge the runtime tdl with the static chain in apply_chain. Re-uses existing dispatch (no gate flip). | Medium. The synthesised tdl never appears in the YAML; troubleshooting "where does this filter come from" requires reading control-plane logs. |
| **B. Per-destination dispatch override** | When ANY edge of a destination has a forced profile, that destination's `use_device_channel` flips to `false` for the rest of the run; the host stage_link path takes over and the new taps apply via the v1-fin-C effective-taps rebuild. | Adds a runtime "dispatch override" flag per destination; trades GPU speed for new-profile correctness on forced edges. | High. Silent perf cliff: a destination's per-slot kernel time can jump 10× when one of its edges flips. Operator may not notice. |
| **C. Document-only (status quo)** | Force keeps its current "stored but inert" semantics; v3.1 adds explicit `event=control_force_warning` whenever force=true is observed, plus a counter, plus a doc paragraph naming the inertness. | Trivial. | None. |

My recommendation is **C** for v3 minimal, **A** as the future expansion if the use case actually materialises. **B** is rejected by the same reasoning v2's plan used: silent dispatch flips are dangerous.

### What v3.1 adds (assuming C)

- `event=control_force_warning link_id=L reason="chain has no leading tdl; profile stored but inert"` emitted at snap time for any forced profile on a non-tdl-leading chain.
- New stats counter `force_inert_warnings`.
- §13 + §21 + the v2 plan get a paragraph naming the inertness explicitly — "force=true does not modify kernel output unless the chain already routes through the per-edge tdl path; use a YAML reload to add the dispatch route".

### If the user picks A or B instead

Each becomes its own implementation phase (~2 days). Outline available on request; the plan defers committing until the user chooses.

## v3.2 — Hardware checks at broker startup

### Use case

The CUDA backend assumes specific GPU shape (SM 12.0 — RTX 5090 — for the bundled tests, but the binary builds for any compute capability ≥ 7.0). A topology that fits in 32 GB of GPU memory will silently OOM on an 8 GB card. A kernel built for SM 12.0 will refuse to launch on SM 7.0. The broker currently surfaces these as "CUDA error 209" or similar opaque errors mid-run; v3.2 catches them at startup with a typed message.

### What changes and what stays

| Component | Today | After v3.2 |
|---|---|---|
| Broker startup | Validates topology shape + creates processor | + runs `probe_cuda_hardware()` before processor creation |
| Output | `event=start backend=cuda cuda_status=ok` | + `event=hardware_probe device=0 name="RTX 5090" sm=12.0 mem_gb=32 pcie_gen=5 pcie_width=4 driver_version="560.x"` |
| Rejection | OOM or kernel-launch failure at first serve | + Reject at startup with `event=fatal reason="hardware: insufficient memory (need X GB, have Y GB)"` or similar typed errors |
| `--hardware-strict` flag | n/a | When set, reject startup if compute capability is below kernel's compile target. Without the flag, log a warning and continue. |

### Probes

- `cudaGetDeviceCount` — at least one device, the configured `runtime.gpu_device` is in range.
- `cudaGetDeviceProperties` — `name`, `major.minor` SM, `totalGlobalMem`, `pcie_link_*`.
- Topology memory footprint estimate (already computed in §15 of the doc): N edges × DeviceLinkState size + N edges × N samples × 8 B + ancillary. Reject if > 80% of `totalGlobalMem`.
- Driver version via `cudaDriverGetVersion`.
- CUDA runtime version via `cudaRuntimeGetVersion`.

### Phasing

| Step | Deliverable | Verification |
|---|---|---|
| HW1 | `probe_cuda_hardware()` function returning a typed struct. Called from broker startup. Emits `event=hardware_probe`. | Unit test (CPU-only build): probe returns "no device" cleanly, broker exits with typed error. |
| HW2 | Memory footprint estimate vs reported `totalGlobalMem`. Reject if over 80%; warn if over 60%. | Unit test with synthesised footprint that exceeds threshold. |
| HW3 | `--hardware-strict` flag; SM-capability mismatch reject. | Unit test that passes/fails based on a mocked SM value. |

## Risks and unknowns

- **Telemetry PUB SNDHWM tuning.** Default 1000 frames in the send queue at 20 Hz × 16 links = 320 frames/s — a 1000-frame queue absorbs 3 seconds of subscriber stall. If real workloads need more (rare), the cap is configurable.
- **`TelemetrySnapshot` torn reads.** Snap writes a non-atomic POD; telemetry thread reads. Using a per-link seqno that brackets the write means the reader can detect a torn read and skip; in practice the structure is small enough that the read window is sub-microsecond — collisions vanish at any reasonable telemetry rate.
- **Force-flag option A's synthesised chain head.** If the snap path can mutate the "effective chain" but the YAML chain stays unchanged, debugging gets harder. Mitigation: every synthesis logs an event with the full effective chain.
- **Hardware probe failures on non-CUDA builds.** `probe_cuda_hardware()` must compile-out cleanly on CPU-only builds (already the pattern for `cuda_compiled()`).
- **Static topology footprint estimate accuracy.** The 80% threshold has to leave headroom for cuBLAS / cuFFT / system allocations the broker doesn't control. 80% is a guess; tune after first incident.

## Out of scope for v3 (named so they don't drift in)

- ZMQ authentication / encryption (CurveZMQ etc.)
- Telemetry rate-limiting per subscriber (the cap is global)
- Adaptive PCIe / kernel-time monitoring (the existing `event=gpu_timings` covers this)
- Runtime hot-reload of `topology.yaml` (separate concern, never in scope)
- Live add/remove of links at runtime (would need v3 + a topology-mutation surface)

## Resolved design decisions (locked 2026-05-26)

1. **Telemetry rate: configurable via `--telemetry-rate-hz`, default 20 Hz.** 20 Hz × 16 links ≈ 320 frames/s on the wire — comfortable. Operator can dial up to per-slot (2000 Hz) for high-resolution traces or down to 1 Hz for low-bandwidth logging.
2. **Telemetry frame shape: one PUB per link, with link_id as ZMQ topic prefix.** Subscribers filter via `setsockopt(ZMQ_SUBSCRIBE, "ue0-gnb0", …)` to receive only that link's frames; empty subscription receives all. Per-frame overhead is small (~200 B) and subscribers only pay bandwidth for the links they asked for.
3. **v3.1 force-flag semantic: Option C (status-quo + explicit warning).** `force=true` stays inert on the data plane. v3.1 adds `event=control_force_warning link_id=L reason="…"` + a `force_inert_warnings` stats counter when force is observed on a non-tdl-leading chain. Honest about the limitation; trivial to ship. Options A (chain-head synthesis) and B (dispatch-override) are deferred until a use case actually materialises.
4. **Hardware-strict mode: off by default; opt-in via `--hardware-strict`.** v3.2 always probes and emits `event=hardware_probe`, and always rejects topologies whose memory footprint exceeds 80% of `totalGlobalMem`. SM-capability mismatch is a warning unless `--hardware-strict` is passed, where it becomes a startup rejection. Easier first-run on heterogeneous workstations.
