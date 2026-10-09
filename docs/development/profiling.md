# Profiling

(section-19)=

(profiling)=

## Profiling — instrumentation system design

The broker is instrumented end-to-end so a real-time relay can be diagnosed from log lines alone, no debugger attach required. The instrumentation answers three questions: *where is the time going*, *where are samples being lost*, and *where is the system at the edge of realtime margin*.

The architecture has **two producers** emitting onto **one stdout event stream**, plus a parallel **microbenchmark CLI** that drives the same processor without ZMQ I/O:

1.  **Host-side `chrono::steady_clock` brackets in the broker server loop** — measure each per-serve stage (wait_req · align · read · process · throttle · send) and publish as `event=cpu_stage_timings`. See Diagram K ([The broker — per-slot loop](../concepts/broker-loop.md#broker-loop)) for the stage definitions.
2.  **CUDA event brackets around the processor's GPU work** — `cudaEventRecord` wraps H2D, kernel, D2H; the host reads the µs deltas after `cudaStreamSynchronize` and publishes them as `event=gpu_timings`. The only layer that sees *inside* the GPU.
3.  **Microbenchmark CLI** (`ocudu-gpu-channel-bench`) — drives the channel processor in a tight loop with no ZMQ I/O and no broker scheduling, emitting per-percentile latency CSV. Shares the same `cudaEvent` instrumentation with the broker — just a different driving cadence. Numbers populate [Performance — measured boundaries](../reports/performance/measured-boundaries.md#perf).

Both producers' events plus ring-pressure heartbeats, lifetime data-integrity counters, and fatals share one stdout stream — see [Event vocabulary](profiling.md#profiling-events) for the full vocabulary. Memory footprint is analytical (computed at `prepare()` time, not measured) — see [Signal memory — pinned host buffers paired with device buffers](../reference/backends.md#signal-memory).

(arr_on)=

(arr_od)=

(arr_on_b)=

(arr_grey)=
 ![Diagram N — instrumentation pipeline, 4 columns. Two drivers (broker = always-on, bench CLI = on-demand) push work through the same processor. Two timing sources wrap that work: chrono::steady_clock for host stages (align / read / pack / throttle) and cudaEvent for the GPU phases (H2D / kernel / D2H). Both µs streams emit as key=value lines on stdout in the broker's vocabulary ( §19.1 ). The harness captures stdout to results/reports/ as CSV + JSON; perf-sweep scripts aggregate per-config p50 / p95 / p99 and populate the measurements in §20 + Diagram W. The bench reuses the same processor + same cudaEvent primitives, so its numbers and the live broker's numbers measure the same underlying work — only the driving cadence differs.](../assets/diagrams/reference-18.svg)

<a href="../assets/diagrams/reference-18.svg">Open this diagram at full size</a>

Diagram N — instrumentation pipeline, 4 columns. Two **drivers** (broker = always-on, bench CLI = on-demand) push work through the same processor. Two **timing sources** wrap that work: `chrono::steady_clock` for host stages (align / read / pack / throttle) and `cudaEvent` for the GPU phases (H2D / kernel / D2H). Both µs streams emit as `key=value` lines on stdout in the broker's vocabulary ([Event vocabulary](profiling.md#profiling-events)). The harness captures stdout to `results/reports/` as CSV + JSON; perf-sweep scripts aggregate per-config p50 / p95 / p99 and populate the measurements in [Performance — measured boundaries](../reports/performance/measured-boundaries.md#perf) + Diagram W. The bench reuses the same processor + same `cudaEvent` primitives, so its numbers and the live broker's numbers measure the same underlying work — only the driving cadence differs.

(profiling-events)=

### Event vocabulary

The broker emits the following event kinds. Anyone reading `broker.log` can watch in real time with `grep -E 'event=(stop|fatal|gpu_timings|cpu_stage_timings)' broker.log`; the heartbeats are usually noise unless something has wedged.

| Event | Fields | Cadence | What it tells you |
|----|----|----|----|
| `event=cpu_stage_timings` | `t, dev, wait_req_us, align_us, read_us, process_us, throttle_us, send_us` | 1 Hz (last-serve snapshot) | Per-stage CPU latency of the broker's per-serve pipeline. `process_us` is the entire processor call (its h2d/kernel/d2h subset is in `gpu_timings` below). `throttle_us` is mostly idle wall-clock pacing. `wait_req_us` is idle waiting for the next REP request. |
| `event=gpu_timings` | `t, h2d_us, kernel_us, d2h_us` | 1 Hz (last-serve snapshot) | Per-phase GPU timings from `cudaEventElapsedTime`. Zero on the CPU backend. `t` is monotonic seconds since broker start. |
| `event=heartbeat` | `t, dev, ring=size/cap, puller[state, pulls, idle, room_stall, last], server[state, serves, idle, data_spin, last]` | 1 Hz, per device | Liveness probe and ring-pressure snapshot. `room_stall` climbing means the puller is blocked on a full ring; `data_spin` climbing means the server is starved. |
| `event=stats` | `rx_requests, rx_starvations, tx_queue_overflows, tx_sequence_gaps, zmq_errors` | at `--strict-realtime` checkpoints | Lifetime data-integrity counters. See [Counter taxonomy](profiling.md#counter-taxonomy) for what each surfaces. |
| `event=stop` | same fields as `stats` plus `tx_pulls` | once, at shutdown | The final reckoning — what every smoke test greps for to verify the run. |
| `event=fatal` | `error="…"` | at most once | Hard failure; broker exits non-zero. |
| `event=control_start` | `endpoint="tcp://*:5559"` | once at startup, if `--control-endpoint` is set | Confirms the runtime-control ZMQ REP socket is bound. Absent on runs without the flag. |
| `event=control_update` | `link_id, param, old, new, seqno` | one line per applied runtime update | Phase 3 v1 mutable params. Reconstructs every accepted shadow write; the matching server-thread snap happens on the next slot boundary. `seqno` is per-edge monotonic. |
| `event=control_error` | `reason="…"` | one line per rejected REQ | Validation failure (unknown link, unknown param, out of range, malformed JSON). The data plane is unaffected — the REP socket replies with `{"ok":false,"error":"…"}` and the broker keeps running. |
| `event=control_warmup_begin` | `slot, link_id, dl_samples, warmup_slots` | when activation resets history | The update zeroed this link's lane histories. Preserving matrix updates emit no new warmup interval. Output samples in the next `warmup_slots` are warmup artefacts; downstream analysis pipelines should skip this window. |
| `event=control_warmup_end` | `slot, link_id` | at the slot crossing the warmup target | Pairs with the preceding `warmup_begin` for this link. The reset interval has ended; numerical comparisons use the declared test tolerance. |
| `event=control_batch_begin` | `id` | one line per batch_begin REQ | Phase 3 v2.3: caller opened a staging slot. Subsequent scalar / profile_swap / matrix_profile_swap REQs carrying the matching `batch_id` stage into this slot instead of applying. |
| `event=control_batch_commit` | `id, link_count, apply_at_slot` | one line per batch_commit REQ | All staged ops applied with a single `take_effect_at_slot`; per-edge slot-index atomicity (different links may tick at slightly different wall-clock times). |
| `event=control_batch_aborted` | `id, dropped_ops` | one line per batch_abort REQ | Caller discarded the staged batch; no shadows touched. The data plane is unaffected. |
| `event=telemetry` | `link_id, slot, seqno, live{…}, profile_active, warmup_until_slot` | per-edge, at `--telemetry-rate-hz` | Phase 3 v3.0: published on the ZMQ PUB socket (`--telemetry-endpoint`). Topic prefix = `link_id`. Not written to stdout; subscribers filter via `ZMQ_SUBSCRIBE`. |
| `event=control_force_warning` | `link_id, reason` | per snap of a profile_swap with `force=true` on a non-tdl-leading chain | Phase 3 v3.1: the profile was stored + activated but the per-sample chain has no Tdl branch to read it. Data-plane output unchanged. Per-edge counter `force_inert_warnings` aggregates these into `event=stop`. |
| `event=hardware_probe` | `ok, device, name, sm, mem_bytes, driver_version, runtime_version` (or `ok=false error="…"`) | once at broker startup, when the CUDA backend is requested | Phase 3 v3.2: typed CUDA hardware report (`probe_cuda_hardware` → `cudaGetDeviceProperties`). On CPU-only builds the event is suppressed; on CUDA builds without a visible device the event reports `ok=false` + a typed error. |
| `event=hardware_footprint` | `estimated_bytes, total_mem_bytes` | once at broker startup, after `event=hardware_probe` succeeds | Phase 3 v3.2: best-effort estimate of the device-side memory the topology will allocate. Startup rejects with `event=fatal` when the estimate exceeds 80% of `total_mem_bytes`. |
| `event=hardware_warning` | `reason="…"` | at startup if runtime SM \< kernel target (without `--hardware-strict`) | Phase 3 v3.2: kernel launches may fail; the broker keeps running so the operator can diagnose. With `--hardware-strict` the same condition becomes `event=fatal`. |

Both per-serve event types (`cpu_stage_timings`, `gpu_timings`) publish the *last* serve's numbers, not a rolling average — the emit happens on the 1 Hz heartbeat thread and just snapshots the worker threads' last-store values. To get a percentile distribution, either pipe the broker log into a rollup script or use the bench CLI which records every iteration.

(counter-taxonomy)=

### Counter taxonomy

The data-integrity counters partition the failure modes a real-time emulator can hit. Each counter is a hard signal except for one:

- **`rx_starvations`** — the server thread waited longer than 5×slot for an incoming edge to deliver new samples. Soft signal: non-zero in development on a noisy host is expected; non-zero under `--strict-realtime` means the host CPU is too slow for the configured sample rate × topology.
- **`tx_queue_overflows`** — the puller could not write a pulled batch into the device's ring (room stall exhausted retries). Indicates the broker is being fed faster than it can serve. Hard signal.
- **`tx_sequence_gaps`** — a server thread observed a cursor pointing before `earliest_sequence()`, meaning the ring discarded samples the cursor still pointed at. The cursor is snapped forward and the gap counted. Hard signal — IQ was lost.
- **`zmq_errors`** — non-recoverable ZMQ errno from any socket. Hard signal — transport broken.
