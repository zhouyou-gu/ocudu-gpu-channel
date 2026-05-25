# Plan — move the per-edge channel work to the device

Status: **draft** · Phase target: **2.x** (after Phase 1.5 ships) · Effort estimate: **~3–4 focused days** · Risk: **medium-high** (architectural shift, bit-exact parity gives way to statistical parity)

## Goal in one sentence

Move `apply_tdl_step_fading()` (multipath + Jakes + LOS) from host-side `stage_link()` to a new device kernel, while keeping topology resolution on host. The device pipeline becomes **raw-source-IQ → channel kernel → superpose kernel → out**, eliminating the per-edge H2D fan-out that currently dominates PCIe.

## What changes and what stays

| Component | Today | After |
|---|---|---|
| Node ↔ edge mapping resolution | Host, once at startup ([broker.cpp:420-435](../../src/broker.cpp#L420-L435)) | Host, once at startup *(unchanged)* |
| `superposition[]` vector per server thread | Host *(unchanged)* | Host *(unchanged)* |
| Per-link state (`tdl_taps`, `tdl_polyphase`, `tdl_fading`) | Host (`LinkModelState` in [cuda_backend.cu:60-78](../../src/cuda_backend.cu#L60-L78)) | **Device** — one `DeviceLinkState` per (dst_node, edge) in global memory, populated once at startup |
| Per-edge channel application (`apply_tdl_step_fading`) | Host, per edge per slot ([delay.h](../../include/ocudu_gpu_channel/delay.h)) | **Device kernel** `apply_channel_kernel<<<...>>>` |
| Cross-slot `delay_line` ring | Host vector ([delay.h:480-492](../../include/ocudu_gpu_channel/delay.h#L480-L492)) | **Device global memory**, rolled by kernel at end of slot |
| H2D payload | N edges × 23 040 × 8 bytes = N × 180 KiB pre-shaped | **M sources × 23 040 × 8 bytes = M × 180 KiB raw**, where M ≤ N (and often M ≪ N) |
| `superpose_kernel` | Reads `device_staged[k·count + idx]` | *Unchanged* — reads what `apply_channel_kernel` wrote |
| CPU backend | Unchanged | Unchanged (remains the reference for parity, statistically) |
| CPU↔CUDA bit-exact parity test | Asserts bit-exact match on TDL fading ([test_processing.cpp](../../tests/test_processing.cpp)) | Replaced with **statistical parity** (mean power, autocorrelation match, K-factor recovery) — bit-exact dropped |

## Why this is the version worth building

Two payoffs make this worth the cost:

1. **PCIe bandwidth reduction at fan-out.** Today every edge ships its own pre-shaped IQ to the device. For a 1 gNB → N UEs downlink, the broker stages N identical-input-different-channel buffers and ships all N. If we ship raw source IQ and apply the channel on device, we ship **M_sources × 180 KiB** instead of **N_edges × 180 KiB**. At 1-to-8 fan-out, that's **8× H2D reduction**. PCIe is the wall today (§21.5 of the technical reference) — this directly buys headroom.

2. **Host headroom for the broker loop.** `stage_link()` currently runs ~50 µs per link per slot. At 16 edges that's ~800 µs of host work per slot, eating into the broker's ZMQ + alignment budget. Pushing it to the device frees the host for the I/O and scheduling work that already dominates `cpu_stage_timings`.

The cost is real but bounded: **lose bit-exact CPU↔CUDA parity**. The replacement (statistical parity) is what wireless emulators normally assert anyway; bit-exact parity is a debugging luxury we've enjoyed because the host runs the math.

## Architectural overview

### Data flow (before vs after)

```
BEFORE
─────────────────────────────────────────────────────────────────────────────────
broker rings (host)  ──►  for each edge: stage_link()  ──►  host_staged[k·N·count]
                          (apply_tdl_step_fading)              │
                                                               │  H2D: N·180 KiB
                                                               ▼
                                                          device_staged
                                                               │
                                                               ▼
                                                          superpose_kernel
                                                          (apply_chain + sum)


AFTER
─────────────────────────────────────────────────────────────────────────────────
broker rings (host)  ──►  pack per source                ──►  host_source_iq[M·count]
                          (no channel work)                    │
                                                               │  H2D: M·180 KiB   ◄── ~8× smaller at fan-out
                                                               ▼
                                                          device_source_iq
                                                               │
                                                               ▼
                                                          apply_channel_kernel
                                                          (multipath + Jakes + LOS)
                                                               │
                                                               ▼
                                                          device_staged       ◄── unchanged shape
                                                               │
                                                               ▼
                                                          superpose_kernel    ◄── unchanged
                                                          (apply_chain + sum)
```

### Per-link state lives entirely on device

```cpp
// New: include/ocudu_gpu_channel/device_channel.h
struct DeviceLinkState {
  // Topology (fixed for the run; ship once at startup)
  int   src_index;             // index into device_source_iq
  int   n_taps;                // 1..K_MAX (default K_MAX=32)
  TapSpec  taps[K_MAX];        // delay_samples, gain_db, phase_rad, is_los, los_k_db, los_angle_rad
  float    polyphase[K_MAX][8];// precomputed Hamming-windowed sinc, shipped once

  // Fading config
  bool     fading_enabled;
  float    f_d_max_hz;
  float    grid_us;
  float    alpha[K_MAX][M_RAYS]; // M_RAYS=20 sub-ray angles per tap, drawn host-side
  float    phi[K_MAX][M_RAYS];   // sub-ray initial phases
  float    phi_los[K_MAX];       // per-tap LOS initial phase

  // Cross-slot state (kernel reads + updates each serve)
  IqSample delay_line[K_MAX + 8 + R_MAX]; // ring with R_MAX = ceil(max tau)
  uint64_t slot_start_samples;  // advances by count per serve
};
```

For K_MAX=32, M_RAYS=20, R_MAX=64 (= ceil(max-delay-samples + headroom)):
- Per-link state ≈ 4 KB
- Per-node total (N=16 edges) ≈ 64 KB
- Per-broker total (4 nodes, 16 edges each) ≈ 256 KB device global memory

Tiny, by GPU standards.

### Kernel design

Two-pass: `apply_channel_kernel` then `superpose_kernel` (existing). The new kernel is the load-bearing addition.

```
Grid: dim3(N_edges_total, ceil(count / 256))
Block: 256 threads

Block (k, b):
  Load DeviceLinkState[k] into shared memory cooperatively (~4 KB)
  Materialize per-tap coarse-grid g_k(t) into shared memory:
    For each tap with fading_enabled:
      For each grid point g in [0, grid_count):
        g_k_rayleigh[k_tap][g] = (1/√M_RAYS) · Σ_m exp(j(ω_m·t_g + φ_m))
  Sync block.
  For thread idx in this block's sample range [b*256, b*256+256):
    For each tap k_tap in 0..n_taps:
      Read x at delay τ_k via polyphase + delay_line (block-shared)
      Interpolate g_k_rayleigh at sample idx
      If is_los: compose Rician (K/(K+1))·specular + (1/(K+1))·rayleigh
      Accumulate y[idx] += 10^(g/20) · e^(jφ) · g_k(t_idx) · x[idx - τ_k]
    device_staged[k·count + idx] = y[idx]
  Sync block. Update delay_line ring in global memory (block leader thread).
  Update slot_start_samples (block leader thread).
```

Then `superpose_kernel` runs as before, reading `device_staged` and summing into output.

### What gets shared, what stays per-block

| Data | Where | Why |
|---|---|---|
| `DeviceLinkState` topology fields (taps, polyphase, alpha, phi) | Shared memory after one-time global load | Read many times per slot by every thread in the block |
| Per-tap coarse-grid g_k(t) (~grid_count × M complex floats) | Shared memory | Built once at slot start, read O(count) times by all threads |
| `delay_line` ring | Global memory, loaded into shared memory at slot start, written back at slot end | Cross-slot continuity requires global persistence; per-slot access is fast via shared |
| `slot_start_samples` | Global memory, one int per link | Updated by block leader at slot end |
| Per-sample (i,q) | Registers | Per-thread work, never spills |

## RNG strategy decision

The host today uses `std::mt19937_64` seeded by `hash("<link_key>:fading:<step_idx>")` to draw alpha (sub-ray angles) and phi (initial phases) once at startup. The device needs the same per-link draws to produce the same channel realisations.

**Two choices:**

| Strategy | Pros | Cons |
|---|---|---|
| **A. Draw alpha/phi on host, ship to device** *(recommended)* | Identical draws as today (parity remains for the random structure, even if per-sample bit-exactness drops). One H2D copy at startup. No need to reimplement mt19937_64 on device. | None significant — adds ~K · M · 2 × 4 bytes per link to startup H2D. Trivial. |
| B. Reimplement mt19937_64 on device | Self-contained device | mt19937_64 is hard to implement efficiently on GPU; would need to port the state engine; no real benefit |

**Recommendation: A.** The host's `prepare_tdl_fading_state` already does this draw; just ship the result.

## Per-link state memory placement decision

| Field | Placement | Rationale |
|---|---|---|
| `taps`, `polyphase`, `alpha`, `phi`, `phi_los` | Global memory, loaded to shared at block start | Read-only per slot, hot during slot, ~3 KB per link |
| `delay_line` ring | Global memory, mirrored into shared at slot start, written back at slot end | Cross-slot persistence required; shared memory access during slot for fast convolution |
| `slot_start_samples` | Global memory, single 8-byte word | Updated once per slot per link by block leader |
| Per-tap coarse-grid `g_k(t)` | Built in shared memory at slot start; never persisted | Recomputable from alpha/phi/slot_start_samples; cheap to regenerate |

Total shared memory budget per block: ~6-8 KB (link state + grid). Within all modern SM shared memory budgets (~48-100 KB available).

## Phased rollout — D1 through D6

### D1 — scaffold (~0.5 day)
- Add `include/ocudu_gpu_channel/device_channel.h` with `DeviceLinkState` and the kernel declaration.
- Add `src/device_channel.cu` with the empty kernel.
- Extend `cuda_backend.cu`'s `prepare()` to allocate `DeviceLinkState[]` on device, populate from each link's host state (including the alpha/phi draws from `prepare_tdl_fading_state`).
- Build clean. No behavioural change yet.

### D2 — static channel (no fading) on device (~1 day)
- Implement `apply_channel_kernel` for the no-fading path only: multi-tap convolution + polyphase, cross-slot `delay_line`.
- Wire `cuda_backend.cu::process_superposition` to use `apply_channel_kernel` instead of host-side `stage_link` **for links with `fading_enabled == false`** (gate by config).
- Validate: existing `test_processing` static TDL tests should still pass within statistical tolerance.
- Run `gpu-test-sequence.sh` steps 3-6 (which use no-fading models) — must still pass.

### D3 — fading on device (~1 day)
- Add the coarse-grid `g_k(t)` materialization (in shared memory at block start).
- Add Rician LOS composition for `is_los` taps.
- Wire the fading path through `apply_channel_kernel`.
- Validate: replace bit-exact parity test with a statistical parity test (mean power within tolerance, autocorrelation within Bessel J₀ envelope).
- Run `gpu-test-sequence.sh` step 7 (TDL-A profile relay) — must pass.

### D4 — source-side rebuffering (~0.5 day)
- Currently the host packs per-edge slots into `host_staged`. Replace with per-source packing: `host_source_iq[src_index][count]`.
- H2D copies `host_source_iq` instead of `host_staged`. Bandwidth reduction kicks in here.
- `apply_channel_kernel` reads `device_source_iq[link.src_index]` instead of `device_staged[k * count]`.
- Validate: same as D3 plus `perf-fanin-sweep.sh` to measure H2D reduction.

### D5 — performance measurement (~0.5 day)
- Run `perf-fanin-sweep.sh` and compare H2D bandwidth + kernel µs against the pre-D4 baseline.
- Update `docs/blueprint-generated/perf-W-pcie-h2d.svg` companion figure with the new H2D curve.
- Update HTML doc §21.5 PCIe bottleneck with the new measurement.

### D6 — full migration + doc sweep (~0.5 day)
- Drop the host-side `stage_link()` path on the CUDA backend (CPU backend unchanged — it still uses `apply_chain_to_link` with the `delay.h` helper).
- Update §11 of the technical reference: the channel now runs **device-side**, not host-side. The narrative we worked so hard to establish ("host-side stage_link before H2D") gets rewritten for Phase 2.
- Update Diagram S to show the new flow.
- Add a §11 historical note that the host-side path is still available on the CPU reference backend.
- Final `gpu-test-sequence.sh` 7/7.
- `ocudu-attach-smoke.sh` to verify real OCUDU + srsUE attach still works.

## Risks and mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **Loss of bit-exact CPU↔CUDA parity** | Medium — it's an explicit project invariant today | Document the change; add statistical parity tests that capture the same wireless behaviour (mean power, autocorrelation J₀, K-factor recovery). Keep CPU backend as the reference for those statistics. |
| **Device RNG drift** (different alpha/phi than host) | Medium-high | Mitigated by Strategy A: draw on host, ship to device. Identical sub-ray realisation by construction. |
| **Cross-slot `delay_line` continuity bugs** | High | Add a determinism test: run the kernel for 100 slots on a fixed input, compare every slot's output across two runs. Must be bit-identical (same kernel, same input → same output). |
| **Shared memory pressure for large K or large M** | Low at current scales; could bite for K_MAX=64 | Profile shared memory occupancy; fall back to global memory for the coarse grid if needed. |
| **Coarse-grid Jakes generator off by a sub-ray phase** | Medium — easy to introduce, hard to spot | Validate against the CPU-generated `g_k(t)` array for at least one slot (ship the CPU result to device and diff). |
| **Two-kernel launch overhead** | Low | Two `cudaMemcpyAsync` are already separate calls; one extra kernel launch is <1 µs. Measure but expect negligible. |
| **CPU backend lags behind in correctness** if device kernel ships a fix | Medium | Keep the `delay.h` helper as the canonical math spec. Any change to device kernel must update `delay.h` and the CPU `apply_chain_to_link` simultaneously. |
| **Real OCUDU path regression** | High — Milestone A is the load-bearing demo | `ocudu-attach-smoke.sh` is mandatory before merging D6. |

## Validation matrix per phase

| Phase | Local ctest | gpu-test-sequence.sh | perf-fanin-sweep.sh | ocudu-attach-smoke.sh |
|---|---|---|---|---|
| D1 | 4/4 | 7/7 (no change) | — | — |
| D2 | 4/4 (statistical parity for static path) | 7/7 (no-fading steps 3-6 must pass) | — | — |
| D3 | 4/4 (statistical parity for fading) | 7/7 (incl. TDL-A) | — | — |
| D4 | 4/4 | 7/7 | run + compare to baseline | — |
| D5 | 4/4 | 7/7 | publish new curves | — |
| D6 | 4/4 | 7/7 | — | must pass |

## Open design questions to resolve before D1

1. **K_MAX, M_RAYS, R_MAX constants** — pick concrete max sizes for the device-side state. Suggested K_MAX=32 (covers all TR 38.901 profiles + headroom), M_RAYS=20 (Zheng-Xiao default, already in delay.h), R_MAX = ⌈max_delay_samples⌉ + 8 (~32 at 100 ns DS, 23.04 MS/s).
2. **Grid materialization strategy** — build the full per-tap coarse grid at slot start (~10 grid points per tap at f_d=100 Hz / grid=100µs over a 1ms slot), or compute lazily during convolution? Recommendation: precompute at slot start. Cheap, simple, no per-sample trig.
3. **CSR vs flat layout for `DeviceLinkState[]`** — keep flat (one struct per link, indexed by global link index) for simplicity. CSR only buys anything if we plan dynamic topology, which is out of scope here.
4. **Constant memory vs global** — `polyphase` is small (~256 bytes per link) and read-only; tempting to put in constant memory. Skip for now — global memory + shared memory caching is simpler and fast enough.
5. **Bypass for `tdl_fading.enabled == false`** — keep the static-channel kernel path simpler than the fading path? Or one unified kernel with a branch? Recommendation: one kernel, branch on `link.fading_enabled`. The fast path costs an extra register or two; complexity stays low.

## What stays unchanged

- CPU backend (`cpu_backend.cpp`) — keeps using `apply_chain_to_link` and `delay.h` helpers verbatim. CPU stays the reference.
- `delay.h` — keeps its current public API. The device kernel implementation may diverge in numerical details (FMA, transcendental approximations) but conforms to the same math spec.
- Topology resolution (`broker.cpp:420-435`) — still host-only, still once at startup. Diagram V's host-side resolution panel is correct as-is.
- `superpose_kernel` — unchanged.
- Broker hot-path — unchanged except for the source-rebuffering in D4.
- All YAML topology examples — unchanged.

## When to actually do this

This plan is queued at **Phase 2** in the roadmap (`AGENT_PROGRESS.md`). The trigger conditions would be:

- A real perf measurement shows PCIe is the dominant bottleneck at the configurations we care about (already true at N≥4 per §21.5).
- We have a real use case for fan-out ≥ 8 in production (e.g., multi-UE OCUDU deployments).
- The bit-exact CPU↔CUDA parity test value is outweighed by the PCIe headroom we'd gain.

Until those trigger, the current host-side design is the right one. This plan is the path we'd take when the trigger fires.

## May-25 perf measurement — trigger HAS fired, but reason has shifted

A fresh `perf-fanin-sweep.sh` on the RTX 5090 (commit `55634fc`, snapshot
at `docs/blueprint-generated/sweep-2026-05-25.json`) sharpened the picture
in ways that change this plan's priorities.

### What stayed the same

GPU-side numbers are byte-for-byte identical to the May-22 baseline.
`h2d_us`, `kernel_us`, `d2h_us`, `gpu_process_us` all unchanged — the
GPU pipeline itself is healthy. PCIe gen-5 x4 utilisation at N=16 is
**86 %** of the 15 GB/s ceiling, consistent with §21.5.

### What changed

`model_mix_latency` (the full chrono wall around `process_superposition`)
exploded vs the May-22 baseline. The growth scales linearly with N edges
and is entirely **host-side**, not GPU-side. The "host gap" (mix −
gpu_process_us):

| Config | gpu_process_us p99 | model_mix_latency p99 | host gap |
|---|---|---|---|
| `one-to-n_N1`  | 58 µs  | 347 µs   | **289 µs**     |
| `one-to-n_N8`  | 157 µs | 2 407 µs | **2 250 µs**   |
| `one-to-n_N16` | 275 µs | 4 771 µs | **4 496 µs**   |
| `one-to-n_N64` | 968 µs | 18 926 µs| **17 958 µs**  |
| **`tdl-a_E2`** | 61 µs  | **7 830 µs** | **7 769 µs** (1 bidirectional pair, TDL-A 23-tap + Jakes) |
| **`tdl-a_E16`**| 157 µs | **57 998 µs**| **57 840 µs** (1 gNB + 8 UEs, TDL-A on all 16 edges) |

`tdl-a_E16` exceeds the 1 ms slot budget by **60×**. This makes any
production TR 38.901 deployment with realistic fan-out **unviable
realtime** on the current host-side path.

### Implications for this plan

1. **The trigger has fired** — but the dominant cost is host-CPU
   stage_link, not PCIe bandwidth. PCIe is real but secondary
   (~228 µs/slot at N=16 vs ~4 500 µs host_gap).

2. **D3 (fading on device) is now the highest-value phase**, not D4
   (source rebuffering). The 58 ms → ~100 µs win on `tdl-a_E16` lives
   in D3. D4 buys PCIe headroom but that's not the wall today.

3. **Sequence remains D2b → D3.** D2b is the smaller, validatable first
   step that proves the device pipeline works end-to-end on the
   static-tdl path. It's a precursor risk-reducer before D3.

4. **D4 demoted to optional polish.** Source-side rebuffering is still
   correct and would still buy ~Nx H2D bandwidth, but only matters
   after D3 closes the host-CPU gap; at that point we should re-measure
   and decide whether D4 is worth the dispatch complexity.

### Open follow-up

The May-22 → May-25 jump in `model_mix_latency` on the simple cuda_mvp
configs (5× slower at the same GPU numbers, same idle box, hot-path
code diff is just comment changes) is unexplained by the diff. Worth a
short investigation before D2b in case it's a pinned-memory first-touch
issue from the new `host_pre_kernel` buffer or an nvcc optimisation
drift. Not blocking — `gpu_process_us` is trustworthy.

### Companion artifacts

- `docs/blueprint-generated/sweep-2026-05-25.json` — full sweep data
- `docs/blueprint-generated/perf-{T,U,W}-*.svg` — regenerated figures
  (overwrite the May-22 versions; previous JSON kept for diff history)

## D2b validation — post-dispatch perf re-measurement

After commit `57edf3d` wired the device-kernel dispatch behind the
per-node `use_device_channel` gate, a fresh sweep on the RTX 5090
(snapshot `docs/blueprint-generated/sweep-2026-05-25-post-d2b.json`)
confirms D2b worked exactly as intended.

### What dropped (cuda_mvp configs — now take the device-kernel path)

| Config | mix p99 pre-D2b | mix p99 post-D2b | Δ |
|---|---|---|---|
| `one-to-n_N1`  | 347 µs    | 76 µs    | **−282 µs (−81%)** |
| `one-to-n_N8`  | 2 407 µs  | 240 µs   | **−2 167 µs (−90%)** |
| `one-to-n_N16` | 4 771 µs  | 437 µs   | **−4 334 µs (−91%)** |
| `one-to-n_N64` | 18 926 µs | 1 621 µs | **−17 305 µs (−91%)** |
| `m-to-n_M16_N16` | 4 798 µs | 442 µs  | **−4 356 µs (−91%)** |

Post-D2b values are within 3-5 % of the **May-22 baseline** (which was
the pre-Phase-2 measurement, taken when host_staged was the only buffer
the host wrote). This means the "mysterious May-22 → May-25 host_gap
regression" flagged in the section above was, in fact, **the host-side
`stage_link()` cost itself** — Phase 1.5 + 1.6 made it visible by
adding more measurement granularity, and Phase 2 D1/D2-C's extra
pinned-memory allocations made it slightly worse via cache/NUMA
pressure on `host_staged`. D2b bypasses stage_link entirely on the
static-tdl path, removing the cost.

### What went up (modest, expected)

| Config | kernel µs pre-D2b | kernel µs post-D2b | Δ |
|---|---|---|---|
| `one-to-n_N1`  | 4.8  | 13.0 | +8.3 |
| `one-to-n_N16` | 16.9 | 27.3 | +10.4 |
| `one-to-n_N64` | 53.6 | 71.8 | +18.1 |

The device kernel now does the multi-tap convolution + polyphase reads
+ delay-line ring touch. +18 µs at N=64 is trivial against the
17 305 µs host saving.

### What didn't change (TDL-A configs — still on host path)

| Config | mix p99 pre-D2b | mix p99 post-D2b | Notes |
|---|---|---|---|
| `tdl-a_E2`  | 7 830 µs   | 7 376 µs  | Within run-to-run noise |
| `tdl-a_E16` | 57 998 µs  | 58 430 µs | Within run-to-run noise |

These configs have `fading_enabled = true`, so the dispatch gate keeps
them on host. **This is exactly where D3 needs to land** — the 58 ms
host work for a 23-tap × 16-edge TDL-A topology is the binding
correctness constraint for any realistic 3GPP deployment.

### D3 priority confirmed

D2b's success validates the device-pipeline architecture (raw IQ H2D →
device channel kernel → device_staged → superpose_kernel) end-to-end
on a real workload. The same dispatch shape extends to the fading path
in D3; the only delta is adding the Jakes coarse-grid materialisation
and Rician LOS composition inside `apply_channel_kernel`. Expected
post-D3 impact on `tdl-a_E16`: 58 000 µs → ~200-500 µs (~120-290×
speedup, into the 1 ms slot budget for the first time).

## D3 result — TDL profiles are realtime-fit for the first time

Commit `b73d8a9` landed the Jakes + Rician LOS device kernel and
broadened the dispatch gate so any all-leading-tdl topology takes the
device path. Snapshot at
`docs/blueprint-generated/sweep-2026-05-25-post-d3.json`.

### TDL profile speedup (the headline)

| Config | pre-D2b | post-D2b | **post-D3** | D3 speedup |
|---|---|---|---|---|
| `tdl-a_E2`  (1 link × 23-tap TDL-A + Jakes 100 Hz) | 7 830 µs | 7 376 µs | **122 µs** | **60×** |
| `tdl-a_E16` (16 edges × 23-tap TDL-A + Jakes 100 Hz) | 57 998 µs | 58 430 µs | **319 µs** | **183×** |

Both configs are now well inside the **1 ms slot budget** (`tdl-a_E16`
at 319 µs has 681 µs of slack). The 60 ms `tdl-a_E16` host bottleneck
that motivated this whole Phase 2 effort is gone.

### Per-phase breakdown (post-D3, tdl-a_E16)

| Phase | µs |
|---|---|
| H2D (raw IQ + steps + step_meta) | 121 |
| Kernel (Jakes grid materialisation + per-sample fading + multi-tap conv + apply_chain + superpose) | 97 |
| D2H | 21 |
| gpu_process_us (cudaEvents H2D_start → D2H_done) | 249 |
| host_gap (cudaStreamSync wait + chrono overhead) | 70 |
| **model_mix_latency** | **319** |

The kernel does the full work for 16 edges × 23 taps × 20 sub-rays × 11
grid points (~80 K complex sinusoid materialisations) plus per-sample
fading interpolation + polyphase convolution + apply_chain + superpose
sum — all in 97 µs on the RTX 5090. The host_gap is now negligible.

### Cuda_mvp configs unchanged

| Config | post-D2b | post-D3 |
|---|---|---|
| `one-to-n_N16` | 437 | 436 |
| `one-to-n_N64` | 1 621 | 1 631 |
| `m-to-n_M16_N16` | 442 | 444 |

The non-fading path is unaffected by D3 (the kernel's fading branch is
skipped when `s->fading_enabled == 0`). Same performance as D2b.

### Correctness — bit-exact tests survived

The existing CPU↔CUDA fading bit-exact test in `tests/test_processing.cpp`
(asserting `require_near_buffer` at 1e-3 tolerance) **passed** on the
remote RTX 5090. The device kernel's `__sincosf` fp32 approximation
accumulated less drift than predicted — within 1e-3 per sample even
after M=20 sub-rays × K=23 taps × polyphase chain. No tolerance bump
needed.

`gpu-test-sequence.sh` step [7/7] avg_power assertion: measured
3.33 / 3.92 (vs expected 3.468, tolerance ±1.5). Comfortably within.

### What's left in Phase 2

- **D4** (source rebuffering) — **LANDED in commit `d3b1a15`**.
  `DeviceLinkState` gained `int src_index`; `CudaSuperposeState` renamed
  `host_pre_kernel`/`device_pre_kernel` → `host_source_iq`/`device_source_iq`
  sized by `num_sources × count` (not `link_count × count`). Host pack
  walks unique sources via `source_first_edge[s]`; kernels read
  `source_iq[s->src_index * count + read_idx]`. Bandwidth saved per slot:
  `(link_count − num_sources) × count` IqSamples.
  **Dormant on production topologies**: the saving fires only when
  multiple edges within one destination's incoming set share a source
  (i.e. multiple links per `(from, to)` pair, e.g. desired + crosstalk
  on the same physical pair). Every current production topology — TDL
  profiles, one-to-N, multi-gNB, stress-16 — has at most one link per
  `(from, to)` pair, so `num_sources == link_count` and the H2D byte
  count is unchanged. The post-D4 `perf-fanin-sweep` confirmed:
  `one-to-n_N8` H2D p99 = 121.0 µs (was 119.4 µs pre-D4 — within
  run-to-run noise). The mechanism is correct (new ctest with 2 edges
  from `gnb0` to `ue0` through different models verifies dedup +
  parity at 1e-3) and is a future-proofing step for multi-model-per-
  pair topologies that haven't shipped yet. Remote
  `gpu-test-sequence.sh` 7/7 PASSED; `ocudu-attach-smoke.sh`
  re-validated Milestone A on the post-D4 build.
- **D5** (perf-measurement formalisation) — done implicitly via the
  sweeps banked here. A fresh `perf-fanin-sweep.sh` on the post-D4 build
  would update Diagram W (PCIe Gbps vs N).
- **D6** (full doc sweep) — landed alongside D4 commit. §6 Diagrams K + V,
  §12 + §12.0 op order, Diagram MF, §20.5 PCIe section, and §21 Planned
  all carry the D4 narrative ("H2D scales with source count, not edge
  count").

Phase 2 is now complete. The hard kernel work landed in D3; D4 closed
the PCIe-duplication loop without breaking the CPU↔GPU parity invariant.

## D7 (considered, not pursued) — source `tx_ring` on the GPU

A more aggressive version of D4 surfaced in discussion: keep the source's
TX ring buffer entirely on the GPU. The host's RX puller would push only
the new samples per slot into the device-side ring; every destination's
`apply_channel_kernel` would read past samples directly from
`device_tx_ring[src_index]` using a cursor + ring-modulo, with no
`DeviceLinkState.delay_line` mirror needed and no per-destination
duplication of source bytes in H2D.

### What D7 would buy

| Property | D7 |
|---|---|
| H2D per slot | One transfer per **unique source**, total ≈ M_sources × 180 KiB (≈ 7× less than today for full-duplex topologies at moderate fan-out — also ≈ 2× less than D4, because D4 still duplicates per destination) |
| `DeviceLinkState.delay_line` | Eliminated — ring on GPU is the single source of truth for source history |
| Max supported tap delay | Effectively unbounded (limited by ring capacity instead of `kDeviceMaxDelayLine = 128`) |
| Architecture | "Single source of truth on GPU" — textbook-clean for the device pipeline |

### Why D7 is not pursued

1. **Breaks the CPU↔GPU symmetry invariant.** The project's load-bearing
   correctness property is that the CPU backend
   (`apply_tdl_step{_fading}` in `delay.h`, host `delay_line` in
   `LinkModelState`) and the GPU backend (`apply_channel_kernel`, device
   `DeviceLinkState.delay_line`) share the **same data-flow shape**. The
   CPU↔CUDA bit-exact parity test (`tests/test_processing.cpp` at 1e-3
   tolerance) depends on this. Moving the ring to GPU forces an
   asymmetric design — CPU keeps its host ring + `delay_line`; GPU has a
   single device ring; the math agrees but the surrounding data flow
   diverges. Two shapes to reason about and to keep tested.

2. **Lost decoupling between destinations.** Today each destination has
   its own `cudaStream_t`, independent of every other. With a shared
   `device_tx_ring` per source, every destination's kernel must wait on
   the source's H2D-write event before it can read. A slow source push
   (network jitter, ZMQ backpressure) blocks **all** of that source's
   destinations instead of just one. This is real downside for the
   broker's strict-realtime gate, which today flags only the directly
   starved destination.

3. **Optimizing what isn't slow.** `tdl-a_E16` H2D is **121 µs** of a
   1000 µs slot budget after D2b+D3 — PCIe is not the binding
   constraint. A 7× H2D reduction would drop `model_mix_latency` from
   319 µs to maybe ~280 µs. Margin gain, not a user-visible win.

4. **Refactor cost.** Broker `Device` struct + RX puller (now writes to
   GPU not host vector), `CudaSuperposeState` (drop
   `device_pre_kernel`), `DeviceLinkState` (drop `delay_line`, add
   `device_src_index`), `apply_channel_kernel` (ring-modulo read instead
   of flat indexed), cross-stream synchronization plumbing. ~2 focused
   sessions plus retest of the bit-exact contract under the new shape.

### Trigger conditions for revisiting

D7 becomes worth doing if **any** of the following fire:

- PCIe gets binding again at higher fan-out than current configs
  exercise (e.g., 64+ edges per destination with realistic channels).
- A use case needs tap delays > 128 samples (e.g., long-range
  propagation models, NTN, or multi-cluster CDL where tap delays span
  many milliseconds at high sample rates).
- The CPU↔GPU bit-exact invariant is intentionally relaxed (e.g., GPU
  becomes the sole reference, CPU backend deprecated).

### The smaller intermediate (also parked)

A subset of D7's win is achievable without breaking CPU/GPU symmetry by
just **eliminating copy #1** (`tx_ring → inputs[k]`) — make
`IqRing`'s storage pinned, so the broker's ring `read()` writes
directly into a pinned host buffer that can be H2D'd without the
`std::copy` into `host_pre_kernel`. Saves ~3 µs × N per serve.
~1 session refactor. Not done because, again, nothing currently
benefits from the µs trim.

### Summary

D7 is the architecturally cleanest device-side design but its costs
(symmetry, decoupling, refactor) exceed the gains (H2D bytes,
elegance) at current project scales. **The current post-Phase 2 design
is the right local optimum** — D4 / D7 / pinned-ring are all real
options to revisit when (and only when) one of the trigger conditions
above fires.
