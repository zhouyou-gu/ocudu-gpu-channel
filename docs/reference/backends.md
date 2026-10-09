# CPU and CUDA backends

(section-10)=

(staged)=

## The staged buffer — packing N edges for one H2D

"Staged" is the word for what the broker hands to the GPU each serve. By the time the broker reaches this stage, [§9](../concepts/timing.md#alignment) has already guaranteed that the `N` per-edge sample arrays are **time-aligned** — every edge's sample at index `idx` represents the same source-time instant, and every array has the same length `serve`. (If a per-edge leading `tdl` step applies, either `apply_channel_kernel` on device or `stage_link()` on host has already run that step's multi-tap convolution + optional Jakes fading using the per-edge `delay_line` ring for cross-slot history — see [§11 Diagram S](device-pipeline.md#kernels) for which path runs when.) **Staging is purely a packing step** — it takes those `N` aligned, delay-applied arrays and concatenates them back-to-back into one pinned buffer (`host_staged`) so a **single** `cudaMemcpyAsync` ships the whole block. The kernel then addresses any edge's any sample with one formula: `staged[k·count + idx]` = edge `k`'s sample `idx`.

```{raw} html
<div class="legacy-diagram"><div class="stage-fig">
<div class="stage-section-label">After §9 alignment — N separate per-edge buffers, all <code>count</code> samples long</div>
<div class="stage-inputs">
<div class="stage-input-row r0">
<span class="stage-edge">edge 0 · ue0→gnb0</span>
<span class="stage-buf">[ s0  s1  s2  s3  ⋯  s_count−1 ]</span>
<span class="stage-size">23 040 IQ · 180 KiB</span>
</div>
<div class="stage-input-row r1">
<span class="stage-edge">edge 1 · ue1→gnb0</span>
<span class="stage-buf">[ s0  s1  s2  s3  ⋯  s_count−1 ]</span>
<span class="stage-size">23 040 IQ · 180 KiB</span>
</div>
<div class="stage-input-vdots">⋮  (one row per incoming edge)</div>
<div class="stage-input-row rN">
<span class="stage-edge">edge N−1 · src→node</span>
<span class="stage-buf">[ s0  s1  s2  s3  ⋯  s_count−1 ]</span>
<span class="stage-size">23 040 IQ · 180 KiB</span>
</div>
</div>
<div class="stage-arrow">pack contiguously into one pinned host buffer</div>
<div class="stage-output">
<div class="stage-section-label">host_staged · one pinned block of <span class="mono">N × count</span> IQ samples</div>
<div class="stage-strip">
<div class="stage-seg s0">edge 0 samples<small>byte offsets [0, count)</small></div>
<div class="stage-seg s1">edge 1 samples<small>byte offsets [count, 2·count)</small></div>
<div class="stage-seg sN">edge N−1 samples<small>byte offsets [(N−1)·count, N·count)</small></div>
</div>
<div class="stage-formula">address formula:  staged[k · count + idx]   →   edge k's sample idx</div>
</div>
<div class="stage-arrow">ONE cudaMemcpyAsync H2D · total = N × count × 8 B</div>
<div class="stage-device">device_staged · same layout · kernel reads staged[k·count + idx]<small>see Diagram B (kernel logic) and Diagram D (buffer pairing) for downstream use</small></div>
</div></div>
```

Diagram J — the "staging" transformation. **Top:** N separate per-edge inputs after time alignment. **Middle:** concatenated back-to-back into one pinned buffer. **Bottom:** a single H2D copies the whole block; the kernel uses one index formula to reach any edge's any sample.

**Why pack at all?**

- **One H2D, not N.** Each `cudaMemcpyAsync` carries fixed per-launch overhead; for N=2 that doubles, for N=8 it's 8×. A single contiguous copy amortises it.
- **Single-pointer kernel argument.** `superpose_kernel` takes one `staged` pointer plus the integer `count`; the address formula derives any (edge, sample) pair without extra metadata.
- **DMA + L2 friendly.** Contiguous memory plays well with the GPU's copy engine and the L2 prefetcher.

**Sizing.** Each IQ sample is 8 bytes (two `float32`). For the canonical 23 040-sample slot and a 2-edge node: `2 × 23 040 × 8 B = 360 KiB per serve`. Even a 16-edge node caps at 2.88 MiB — comfortably under any current GPU's L2 (50 MiB on H100, 128 MiB on B200). For RTX 5090's wider memory bus, 360 KiB clears L2 in tens of microseconds.

(section-14)=

(backends)=

## Processor backends — CPU and CUDA

There are two backends behind a common `ChannelProcessor` interface (`include/ocudu_gpu_channel/processing.h`): **CPU** for correctness reference and local development, **CUDA** for production scale. Both expose a single entry point:

- `process_superposition(dst_node, edges, rx_model, rate, out)` — shapes every incoming edge of `dst_node` through that edge's own channel model, sums them, and applies the node's optional `rx_model` once to the sum. The broker uses this for N-edge fan-in; the benchmark and unit tests use the same call with a one-element `edges` list (the N = 1 single-edge case) instead of a separate per-edge API.

Bit-exactness contract: for any single edge, `process_superposition` on CPU and CUDA produces identical `cf32` output within 1e-3 per sample. This is enforced by tests and matters because it lets us debug regressions on the CPU path before reproducing on the GPU. The shared inline implementation of `apply_tdl_step` + `apply_tdl_step_fading` in `include/ocudu_gpu_channel/delay.h` and the counter-based Philox RNG are what make this practical.

**Both backends implement the same step surface.** The CPU backend's `process_superposition` calls the same `apply_tdl_step_fading` helper as the device-channel kernel, so multi-tap convolution + Jakes Doppler + Rician LOS all work on CPU too — just slower. The 1e-3 parity tolerance accommodates one cycle of trig precision difference (`std::cos(float)` on CPU vs `__sincosf` on GPU); pre-trig angles are computed in `double` and reduced mod 2π on both sides so long-running parity holds. The CPU backend is therefore the trust anchor: every feature CUDA ships, CPU runs first and the test suite asserts they match.

(section-15)=

(signal-memory)=

## Signal memory — pinned host buffers paired with device buffers

The per-slot pipeline that [§11.0](device-pipeline.md#pipeline) describes moves data between two halves of a memory map: a **pinned host side** (allocated with `cudaHostAlloc` so cudaMemcpyAsync is truly asynchronous over PCIe DMA) and a matching **device side** (`cudaMalloc` in GPU global memory). Each state owns its own `cudaStream_t` (non-blocking) plus a quartet of `cudaEvent_t`s that bracket H2D / kernel / D2H for the `event=gpu_timings` log line.

Two state structs hold the broker's signal memory: `CudaSuperposeState` (one per destination node — the broker's only hot-path state) and `CudaLinkSlot` (one per emulator edge, holding the per-edge chain phase / AWGN counter / delay_line ring). The `process_superposition` call walks every incoming edge's `CudaLinkSlot` to pack the staged buffer, then does one fused H2D + kernel + D2H against the per-destination superpose state.

(ard)=
 ![Diagram D — signal memory map for the broker's superposition hot path. Pinned host buffers pair with device global buffers; the small heap-resident chain metadata gets its H2D piggy-backed on the staged buffer's transfer. CudaLinkSlot holds only the per-edge chain-running state (phase / AWGN counter / delay_line) — no scratch.](../assets/diagrams/reference-16.svg)

Diagram D — signal memory map for the broker's superposition hot path. Pinned host buffers pair with device global buffers; the small heap-resident chain metadata gets its H2D piggy-backed on the staged buffer's transfer. `CudaLinkSlot` holds only the per-edge chain-running state (phase / AWGN counter / delay_line) — no scratch.

**Analytical footprint per RX node**. For `N` incoming edges, a chain of `S` steps, and a batch of `count` IQ samples (8 bytes each):

- **Staged buffer** — `8 · N · count` bytes, allocated *twice*: pinned on host, mirrored on device. At `N = 8`, `count = 23 040`: ~1.4 MiB per side.
- **Output buffer** — `8 · count` bytes per side. ~180 KiB.
- **Steps buffer** — `sizeof(GpuStep) · N · max_steps` bytes per side. At `sizeof(GpuStep) = 24`, `max_steps = 5`: ~960 B per N=8 node — tiny.
- **delay_line ring** — per edge, `8 · (d + 2)` bytes host-only, where `d` is the chain-leading delay in samples. Negligible unless `d` is huge.

Total device footprint for an 8-incoming-edge node at 23.04 MS/s fits comfortably in any GPU's L2 (50 MB on H100, 128 MB on B200), and even the full 16-edge stress topology stays in L2. Bandwidth, not capacity, is the working constraint — quantified in [§20.6](../reports/performance/measured-boundaries.md#perf-pcie).

(section-16)=

(stream-concurrency)=

## Multi-stream concurrency — overlapping serves

Each destination node has its own `cudaStream_t` + thread, so the per-serve H2D → kernel → D2H sequence runs concurrently across nodes. With three nodes the GPU is rarely idle: while one node's kernel is running, another's H2D and a third's D2H can be in flight on the GPU's copy engine.

```{raw} html
<div class="legacy-diagram"><div class="gantt" style="max-width:760px; margin:0 auto;">
<div class="gantt-header">
<span></span>
<div class="gantt-tick-row">
<span class="gantt-tick" style="left:0%">t=0</span>
<span class="gantt-tick" style="left:25%">50 µs</span>
<span class="gantt-tick" style="left:50%">100 µs</span>
<span class="gantt-tick" style="left:75%">150 µs</span>
<span class="gantt-tick" style="left:100%">200 µs (zoomed)</span>
</div>
</div>
<div class="gantt-row">
<div class="gantt-label">gnb0 stream</div>
<div class="gantt-track">
<div class="gantt-bar gantt-bar--h2d" style="left:0%; width:13.5%;">H2D</div>
<div class="gantt-bar gantt-bar--kernel" style="left:13.5%; width:27%;">apply_channel + superpose</div>
<div class="gantt-bar gantt-bar--d2h" style="left:40.5%; width:7%;">D2H</div>
</div>
</div>
<div class="gantt-row">
<div class="gantt-label">ue0 stream</div>
<div class="gantt-track">
<div class="gantt-bar gantt-bar--h2d" style="left:5%; width:13.5%;">H2D</div>
<div class="gantt-bar gantt-bar--kernel" style="left:18.5%; width:27%;">apply_channel + superpose</div>
<div class="gantt-bar gantt-bar--d2h" style="left:45.5%; width:7%;">D2H</div>
</div>
</div>
<div class="gantt-row">
<div class="gantt-label">ue1 stream</div>
<div class="gantt-track">
<div class="gantt-bar gantt-bar--h2d" style="left:10%; width:13.5%;">H2D</div>
<div class="gantt-bar gantt-bar--kernel" style="left:23.5%; width:27%;">apply_channel + superpose</div>
<div class="gantt-bar gantt-bar--d2h" style="left:50.5%; width:7%;">D2H</div>
</div>
</div>
<div class="gantt-legend">
<span><span class="gantt-swatch" style="background:#5db1ff; opacity:.55;"></span>cudaMemcpyAsync H2D (copy engine)</span>
<span><span class="gantt-swatch" style="background:#5db1ff;"></span>apply_channel + superpose (compute engine)</span>
<span><span class="gantt-swatch" style="background:#5db1ff; opacity:.30;"></span>cudaMemcpyAsync D2H (copy engine)</span>
</div>
</div></div>
```

Diagram I — per-node CUDA streams overlap, zoomed to the first 200 µs of the slot (the 500 µs / 1 ms slot deadline is far to the right, off-chart). Bars abbreviate `apply_channel_kernel` as *apply_channel* and `superpose_kernel` as *superpose* to fit the track. Bar widths reflect the `tdl-a` profile measurement on RTX 5090: H2D ≈ 27 µs, `apply_channel_kernel` + `superpose_kernel` ≈ 54 µs, D2H ≈ 14 µs per stream (May 2026 perf-backend-compare run). The GPU's copy engines and compute engine run concurrently, so one node's D2H and another's H2D coexist on the PCIe bus while a third's kernel executes — total per-slot wall time stays around 120 µs even with three nodes serving in parallel.

(part-iv)=
