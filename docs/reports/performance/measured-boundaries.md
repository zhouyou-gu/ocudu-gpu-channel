# Measured performance boundaries

> Historical evidence migrated from the technical reference at `58d3156`. Measurements retain their original setups and limits; see [current status](../../getting_started/status.md) for the qualification summary.

(section-20)=

(perf)=

## 20. Performance — measured boundaries

All numbers in this section come from runs on an **NVIDIA GeForce RTX 5090** (driver 570.211.01, 32 GB VRAM, CUDA 12.8.1) via `scripts/remote/perf-sweep.sh`, `scripts/remote/perf-fanin-sweep.sh`, and `scripts/remote/perf-backend-compare.sh`. The bench (`ocudu-gpu-channel-bench`) drives the same `ChannelProcessor::process_superposition()` entry point the live broker uses — one fused call per RX node per slot under CUDA, with the CPU backend looping per edge internally and summing on the host. All µs values are `p99` over a 5–10 s run; verdict band per [§21.1](../../guides/broker.md#latency-gate) (slot deadline = 500 µs at 30 kHz SCS, 1 ms at 15 kHz SCS).

(perf-phase2)=

### 20.0 Phase 2 headline — TDL profiles realtime-fit

The Phase 2 device-channel pipeline moved the per-edge channel (multi-tap convolution + Jakes Doppler + Rician LOS) from a host-side loop into `apply_channel_kernel` on the GPU ([§11 Diagram S](../../reference/device-pipeline.md#kernels)). For the first time, the 3GPP TR 38.901 TDL profiles fit inside the slot deadline by an order of magnitude — and the CPU↔CUDA speedup grew from "10× on a single-tap toy" to "30–46× on a realistic 23-tap fading channel". Data below from `perf-backend-compare-2026-05-25` on the RTX 5090.

<table style="width:100%;">
<colgroup>
<col style="width: 14%" />
<col style="width: 14%" />
<col style="width: 14%" />
<col style="width: 14%" />
<col style="width: 14%" />
<col style="width: 14%" />
<col style="width: 14%" />
</colgroup>
<thead>
<tr>
<th>Matrix</th>
<th>config</th>
<th>CPU p99 (µs)</th>
<th>CUDA mix p99 (µs)</th>
<th>CUDA kernel p99 (µs)</th>
<th>speedup</th>
<th>verdict @ 500 µs / 1 ms</th>
</tr>
</thead>
<tbody>
<tr>
<td rowspan="3"><strong>Synthetic fan-in</strong><br />
<span class="small">single-tap tdl + phase + cfo</span></td>
<td><code>one-to-n_N1</code></td>
<td>627</td>
<td><strong>121</strong></td>
<td>13</td>
<td><strong>5.2×</strong></td>
<td>green / green</td>
</tr>
<tr>
<td><code>one-to-n_N4</code></td>
<td>2269</td>
<td><strong>191</strong></td>
<td>15</td>
<td><strong>11.9×</strong></td>
<td>green / green</td>
</tr>
<tr>
<td><code>one-to-n_N16</code></td>
<td>8927</td>
<td><strong>480</strong></td>
<td>27</td>
<td><strong>18.6×</strong></td>
<td>yellow / green</td>
</tr>
<tr>
<td rowspan="5"><strong>TR 38.901 TDL profiles</strong><br />
<span class="small">23-tap + Jakes 100 Hz<br />
LOS on D/E</span></td>
<td><code>tdl-a</code> (NLOS)</td>
<td>7365</td>
<td><strong>167</strong></td>
<td>54</td>
<td><strong>44×</strong></td>
<td>green / green</td>
</tr>
<tr>
<td><code>tdl-b</code> (NLOS)</td>
<td>7327</td>
<td><strong>168</strong></td>
<td>54</td>
<td><strong>44×</strong></td>
<td>green / green</td>
</tr>
<tr>
<td><code>tdl-c</code> (NLOS)</td>
<td>7781</td>
<td><strong>168</strong></td>
<td>56</td>
<td><strong>46×</strong></td>
<td>green / green</td>
</tr>
<tr>
<td><code>tdl-d</code> (LOS K=13.3 dB)</td>
<td>4643</td>
<td><strong>152</strong></td>
<td>41</td>
<td><strong>30×</strong></td>
<td>green / green</td>
</tr>
<tr>
<td><code>tdl-e</code> (LOS K=22 dB)</td>
<td>4468</td>
<td><strong>157</strong></td>
<td>43</td>
<td><strong>28×</strong></td>
<td>green / green</td>
</tr>
</tbody>
</table>

**Read in one line.** Every TDL profile that was **red on CPU** (4.5 – 7.8 ms per call, ≈ 10× the 500 µs / 30 kHz slot) is now **green on CUDA** (152 – 168 µs, well under both 30 kHz and 15 kHz deadlines). The per-edge channel kernel envelope is ~40–56 µs depending on whether the tap set is LOS-bearing; H2D is the next cost at ~27 µs. The bench's CPU↔CUDA `avg_power_cum` match within ±1.5 power units per RX (statistical-equivalence guard at `perf-backend-compare.sh` \[7/7\]) — i.e. CUDA isn't running a different channel.

The headline tdl-a_E16 measurement called out elsewhere in this doc (1 gNB + 8 UEs, TDL-A 23-tap + Jakes 100 Hz on all 16 edges, **58 430 µs host → 319 µs device — 183×**) is the same kernel under heavier fan-in. §20.4–§20.8 below use the older single-tap baseline data; treat those as the **fan-in scaling reference** rather than as the headline.

(perf-mimo)=

### 20.1 Multi-port cost — what a matrix channel adds

Measured on **4×RTX 5090** (driver 570.211.01, CUDA 12.8.93, sm_120), at **23.04 MS/s** with a **23040-sample batch**, **CUDA** backend, over a **3 s** run, `p99`:

| Topology / chain | Kernel p99 | Note |
|----|----|----|
| 1×1, TDL-A 23-tap + Jakes 100 Hz | 53.599 µs | after the generator split; was 61.856 µs before |
| 16 edges, TDL-A 23-tap + Jakes 100 Hz | 69.536 µs | was 90.655 µs before |
| 2×2 spatial correlation, on top of the above | +4.2 µs | the mixing factor is computed once at load, not per slot |

The shape to take from that table: a physical link's cost scales with its **lanes**, because a lane is an ordinary edge to the kernel — a 2×2 is four edges, not a new kind of work — and the correlation mixing is a per-slot cost of a few microseconds because its LDL<sup>H</sup> factor is computed once when the topology loads.

On the live two-port gate (real OCUDU gNB, four ZMQ endpoints, 20 s, same rate and batch), the channel-processor call is sampled once per second by the broker's heartbeat: **median 104.4 µs** against the 1 ms slot of the fixture's 15 kHz SCS, with H2D / kernel / D2H medians of 36.8 / 12.9 / 27.4 µs. The observed maximum in those samples reaches ~1.4 ms; that tail is host scheduling and driver submit-or-sync rather than the channel path, it reproduces without the matrix, and the gate records it instead of scoring the host on it.

(perf-method)=

### 20.2 Methodology and test matrices

Two complementary test matrices are run. The **baseline matrix** (5 configs) covers the topology shapes used elsewhere in the project; the **fan-in sweep** (21 configs, generated by `scripts/tools/gen_topology.py`) isolates how a single RX node's per-call cost scales with edge count, and exercises both fan-in axes of the broker.

<table>
<thead>
<tr>
<th>Matrix</th>
<th>Config</th>
<th>Topology / shape</th>
<th>Edges</th>
<th>Busiest fan-in</th>
</tr>
</thead>
<tbody>
<tr>
<td rowspan="5">Baseline (5)</td>
<td><code>mvp-2-edge</code></td>
<td>1 gNB ↔︎ 1 UE — single bidirectional link</td>
<td>2</td>
<td>1</td>
</tr>
<tr>
<td><code>multi-ue-4-edge</code></td>
<td>1 gNB + 2 UEs, all pairs DL+UL</td>
<td>4</td>
<td>2</td>
</tr>
<tr>
<td><code>graph-6-edge</code></td>
<td>3-node graph with UE↔︎UE crosstalk (Diagram G)</td>
<td>6</td>
<td>2</td>
</tr>
<tr>
<td><code>multi-gnb-8-edge</code></td>
<td>2-cell 4-node multi-gNB with inter-cell interference</td>
<td>8</td>
<td>2</td>
</tr>
<tr>
<td><code>stress-16-edge</code></td>
<td>9-node fan-in star: one sink with 8 incoming + 8 outgoing</td>
<td>16</td>
<td>8</td>
</tr>
<tr>
<td rowspan="2">Fan-in sweep (21)</td>
<td><strong>Set 1: 1-to-N</strong></td>
<td>1 gNB ↔︎ N UEs, no UE↔︎UE crosstalk</td>
<td>2 N</td>
<td><strong>N</strong> (gNB)</td>
</tr>
<tr>
<td><strong>Set 2: M-to-N</strong></td>
<td>M gNB ↔︎ N UE bipartite, no inter-gNB / inter-UE crosstalk</td>
<td>2·M·N</td>
<td><strong>max(M, N)</strong></td>
</tr>
</tbody>
</table>

Set 1 sweeps `N ∈ {1, 2, 4, 8, 16, 32, 64}` with M = 1 (7 configs). Set 2 sweeps the symmetric points `(M = N) ∈ {2, 4, 8, 16}` plus 10 asymmetric pairs for a total of 14 configs.

**What the bench measures vs what the broker does**. The bench is single-threaded and runs every RX node's `process_superposition()` call sequentially within one iteration, then loops. Its `p99` therefore reflects the *worst single call* across the topology — usually the device with the largest incoming-edge count. The live broker, by contrast, dispatches per-node calls on independent CUDA streams (Diagram I) that overlap on the GPU and overlap with ZMQ I/O on the host. **The bench is a per-call cost ceiling; the broker's per-slot wall-clock cost is always ≤ the bench's worst call.** §20.7 demonstrates this empirically.

**API-unification note**: the bench's earlier `--mode per-edge` flag drove a now-removed `process_into()` entry point — see §2 and §14 for the full context. Only `process_superposition()` exists today; a single-edge call is just `process_superposition(dst, {edge}, nullptr, …)`.

(perf-cpu-cuda)=

### 20.3 CPU vs CUDA — the baseline matrix + fan-in sweep

Two views of the same comparison. The **baseline matrix** (left) runs five named topologies that recur elsewhere in the project; the **fan-in sweep** (right) isolates how a single RX node's per-call cost scales as the edge count grows from 1 to 64. Both use the unified `process_superposition` path; CUDA is one fused launch per node per slot, CPU loops per edge and sums on the host. Speedup widens monotonically with edge count.

#### Baseline matrix — five named topologies

| config | backend | p99 µs | h2d p99 | kernel p99 | d2h p99 | verdict | speedup |
|----|----|----|----|----|----|----|----|
| `mvp-2-edge` | CPU | **295.1** | — | — | — | yellow | 1.0× |
|  | CUDA | **64.6** | 26.3 | 4.8 | 17.7 | green | **4.6×** |
| `multi-ue-4-edge` | CPU | **793.0** | — | — | — | red | 1.0× |
|  | CUDA | **107.3** | 40.3 | 6.1 | 19.7 | green | **7.4×** |
| `graph-6-edge` | CPU | **832.7** | — | — | — | red | 1.0× |
|  | CUDA | **92.5** | 40.9 | 6.9 | 19.7 | green | **9.0×** |
| `multi-gnb-8-edge` | CPU | **696.8** | — | — | — | red | 1.0× |
|  | CUDA | **99.7** | 41.3 | 5.8 | 20.7 | green | **7.0×** |
| `stress-16-edge` | CPU | **2236.3** | — | — | — | red | 1.0× |
|  | CUDA | **226.7** | 118.8 | 10.3 | 17.7 | yellow | **9.9×** |

#### Fan-in sweep — synthetic 1-to-N (single RX node receiving from N TX sources)

Topology generator (`scripts/tools/gen_topology.py one-to-n N`) built configs from N = 1 to N = 64. CPU column populated from `perf-backend-compare` (May 25 2026) at N ∈ {1, 4, 16}; CUDA column at every N ∈ {1, 2, 4, 8, 16, 32, 64} from the post-D4 `perf-fanin-sweep`. Verdict band at the 500 µs / 30 kHz slot.

| N (edges) | CPU p99 µs | CPU verdict | CUDA p99 µs | CUDA h2d p99 | CUDA kernel p99 | CUDA d2h p99 | CUDA verdict | speedup |
|----|----|----|----|----|----|----|----|----|
| **1** | 627 | red | **121.0** | 28.7 | 13.0 | 19.7 | green | **5.2×** |
| **2** | — | — | **138.0** | 38.9 | 13.0 | 21.0 | green | — |
| **4** | 2269 | red | **191.5** | 68.9 | 15.1 | 20.9 | yellow | **11.9×** |
| **8** | — | — | **283.2** | 121.0 | 18.8 | 21.0 | yellow | — |
| **16** | 8927 | red | **478.7** | 228.4 | 28.5 | 21.1 | yellow | **18.6×** |
| **32** | — | — | **866.4** | 445.2 | 43.4 | 21.1 | red | — |
| **64** | — | — | **1676.7** | 886.0 | 73.9 | 21.1 | red | — |

**Read this in one line.** CPU is red the moment N reaches 4 and grows roughly linearly with N (~140 µs per edge). CUDA stays green through N = 2, yellow through N = 16, and crosses red at N = 32 because H2D — not the kernel — saturates the PCIe (PCI Express) 5.0 x4 link ([§20.6](measured-boundaries.md#perf-pcie), [Diagram W](measured-boundaries.md#perf-pcie)). The kernel itself only grows from 13 µs at N = 1 to 74 µs at N = 64 — the channel work scales but stays well within budget; the bottleneck is the link, not the math. Speedup widens from 5.2× at N = 1 to **18.6× at N = 16**, the highest CPU-measured point. At N = 32 and N = 64 the CPU is too slow to measure cleanly in a 5 s run; the CUDA-only rows are still useful as the fan-in scaling reference even though the speedup column is unfilled.

![Diagram O — p99 model_mix latency per config, CPU vs CUDA on the unified process_superposition path. CPU crosses the 500 µs slot deadline at 4+ edges. CUDA stays in green through 8 edges and yellow at 16. The CPU/CUDA speedup widens with edge count — from 4.6× at the single-edge config to 9.9× at the 16-edge stress topology — because the CUDA path fuses all edges into one launch while CPU loops linearly.](../../assets/diagrams/reference-19.svg)

<a href="../../assets/diagrams/reference-19.svg">Open this diagram at full size</a>

Diagram O — p99 model_mix latency per config, CPU vs CUDA on the unified `process_superposition` path. CPU crosses the 500 µs slot deadline at 4+ edges. CUDA stays in green through 8 edges and yellow at 16. The CPU/CUDA speedup widens with edge count — from 4.6× at the single-edge config to 9.9× at the 16-edge stress topology — because the CUDA path fuses all edges into one launch while CPU loops linearly.

(perf-bottleneck)=

### 20.4 Where the latency goes — per-phase breakdown

The CUDA per-call cost decomposes into three components that scale differently with edge count:

- **H2D dominates and scales linearly with N** — staged buffer is `N × 23 040 × 8 B`; transfer time grows ~1:1 with edge count (26 µs at N=1 → 119 µs at N=8 → 884 µs at N=64). At large N it saturates the PCIe link ([§20.6](measured-boundaries.md#perf-pcie)).
- **Kernel grows sub-linearly** — one fused launch per RX node; each thread loops N incoming edges in registers. The reported `kernel_us` p99 grows 5 µs → 11 µs → 54 µs as N goes 1 → 8 → 64, but most of that is queueing behind the H2D — actual kernel runtime measured by nsys is 1.7 µs median at N=8 (see [§20.5](measured-boundaries.md#perf-resource)).
- **D2H is a fixed floor** ≈ 19 µs — the output is one batch regardless of N.
- **CPU backend** grows linearly with incoming-edge count (295 → 800 → 700 → 2 240 µs as N goes 2 → 4 → 8 → 16). Bounded by the per-sample chain loop — CFO's `sin/cos`, gain/phase multiplies, AWGN's Philox draw if present. Saturates realtime at ~2 edges per RX. Reference path only.

Diagram T quantifies all three phases against N out to 64. The N=4 config is the last **green** point (p99 = 135 µs); N=16 is the last **yellow** point (425 µs, just under the 500 µs slot); **N=32 is the first red** (810 µs).

![Diagram T — Set 1 fan-in scaling at one RX node. Total p99 is roughly linear in N: doubles for each doubling of N. H2D dominates (~55 % of total at every N); kernel grows but stays small (5 → 54 µs); D2H is flat at ~19 µs. The 1-to-N sweep confirms the bench-mode boundary exactly: N = 16 is the last yellow point (425 µs), N = 32 is the first red (810 µs) .](../../assets/diagrams/reference-20.svg)

<a href="../../assets/diagrams/reference-20.svg">Open this diagram at full size</a>

Diagram T — Set 1 fan-in scaling at one RX node. Total p99 is roughly linear in N: doubles for each doubling of N. H2D dominates (~55 % of total at every N); kernel grows but stays small (5 → 54 µs); D2H is flat at ~19 µs. The 1-to-N sweep confirms the bench-mode boundary exactly: **N = 16 is the last yellow point (425 µs), N = 32 is the first red (810 µs)**.

(perf-resource)=

### 20.5 Compute & memory utilisation — the GPU is half idle

A deep profile run on the stress-16 config (16-edge fan-in at one sink) measures what's actually limiting the GPU. Captured via `scripts/remote/perf-deep-profile.sh` with `nvidia-smi dmon` sampled at 1 Hz alongside `nsys profile` for kernel-level tracing.

#### Memory footprint — measured vs. analytical

| Where | Idle | During stress-16 bench | Postrun | Notes |
|----|----|----|----|----|
| GPU VRAM (32 607 MiB total) | 547 MiB | **1 061 MiB** | 547 MiB | Bench's device allocation = 514 MiB. Postrun returns to idle — clean teardown. |
| Host RSS (bench process) | — | **122 MiB** | — | Includes pinned host_staged, host_output, plus bench scratch + libzmq + libcuda. |

Compared to the analytical estimate in [§15](../../reference/backends.md#signal-memory): the stress-16 sink alone needs ~1.4 MiB staged host + 1.4 MiB device + 180 KiB output = under 3 MiB per node. With 9 nodes preallocated by `prepare()`, the live working set is well under 30 MiB. The rest of the 514 MiB device alloc is CUDA runtime context, libcudart arenas, and pinned-host mirror buffers — overhead, not signal. **Capacity is never close to a constraint** at any realistic scale.

#### Parallel resource utilisation — compute headroom

| Metric | Value during stress-16 bench | Source |
|----|----|----|
| SM utilisation (median) | **56 %** | `nvidia-smi dmon -s u` |
| Memory subsystem utilisation | 0 % | `nvidia-smi dmon -s u` — kernel doesn't stress GDDR7 |
| Graphics clock (active boost) | 2 392 MHz | `nvidia-smi --query-gpu=clocks.current.graphics` |
| Memory clock | 13 801 MHz (full GDDR7 boost) | `nvidia-smi` |
| Power draw (active) | 54 W (vs 575 W TDP — 9 %) | `nvidia-smi --query-gpu=power.draw` |
| `superpose_kernel` actual GPU runtime | **1.7 µs median, 2.3 µs avg, 7.6 µs max** (30 309 launches over 3 s) | `nsys cuda_gpu_kern_sum` |
| `cudaMemcpyAsync` avg call overhead | 1.5 µs (host side) | `nsys cuda_api_sum` |
| `cudaLaunchKernel` avg call overhead | 2.2 µs (host side) | `nsys cuda_api_sum` |
| `cudaStreamSynchronize` avg wait | 45 µs (waiting for the GPU pipeline to drain) | `nsys cuda_api_sum` |

**Compute is wildly underused on this kernel**. SMs sit at 56 %, GDDR7 is untouched, power draw is 9 % of TDP, graphics clock is at boost. The kernel runs in 1.7 µs and uses ~720 warps (23 040 threads / 32) — fits on 6 of the 170 SMs at 100 % occupancy, leaving 164 SMs idle per launch. Host API overhead is small too (~9 µs/call). **The bottleneck is everywhere except the GPU.**

ncu (Nsight Compute) per-kernel deep metrics (SM throughput, warp-issue rate, achieved occupancy) require setting `NVreg_RestrictProfilingToAdminUsers=0` or running with root — not available on the measurement workstation. The numbers above come from `nsys profile` + `nvidia-smi dmon`, which work unprivileged.

(perf-pcie)=

### 20.6 The PCIe bottleneck — host↔device link is the wall

**Hardware finding: the link negotiated as gen 5 x4, not x16**, on this workstation. The RTX 5090 silicon supports x16 (sysfs `max_link_width = 16`), so the cap is the slot's electrical routing or BIOS bifurcation. This cuts the theoretical ceiling 4× from what an RTX 5090 could otherwise do.

**D4 source rebuffering (landed) is dormant on current production topologies.** The mechanism is in place — H2D payload scales with *unique source count* instead of edge count, indexed per edge via `DeviceLinkState::src_index` — and the new ctest (2 edges from `gnb0` to `ue0` through different models) verifies the dedup is correct. But the byte-level win only materializes when **multiple edges within one destination's incoming set share a source** (i.e. multiple edges exist for the same `(from, to)` pair, e.g. a desired-path edge plus a crosstalk-model edge). Every current production topology — including every TDL profile, the one-to-N fan-in, the multi-gNB graph, and stress-16 — has at most one edge per `(from, to)` pair, so `num_sources == link_count` at every destination and D4 saves zero H2D bytes. The post-D4 `perf-fanin-sweep` re-measured H2D at `one-to-n_N8` at **121.0 µs** vs the pre-D4 **119.4 µs** — within run-to-run noise, confirming the topologies tested here don't benefit. D4 is a correct future-proofing step for multi-model-per-pair topologies that haven't shipped yet.

| Direction | Bytes / call | Bench p99 µs | Achieved Gbps (p99) | Ceiling (gen 5 x4) | Utilisation |
|----|----|----|----|----|----|
| H2D (raw IQ, N = 8) | 1.40 MiB (8 unique sources × 23 040 × 8 B) | 121.0 | 97.5 | ≈ 126 Gbps | **77 %** |
| D2H (RX output) | 180 KiB (23 040 × 8 B) | 21.0 | 70.2 | ≈ 126 Gbps | **56 %** |

Both rows are `one-to-n_N8` from the post-D4 `perf-fanin-sweep` on commit `d3b1a15`. H2D is at 77 % because each `process_superposition` call issues three H2D memcpys (`device_source_iq`, `device_steps`, `device_step_meta`); the two small ones don't amortise. Diagram W traces the saturation curve across the full 1-to-N sweep. The pre-D4 N = 8 H2D was 119.4 µs / 98.7 Gbps — within run-to-run noise of the post-D4 numbers above, as expected since one-to-N has one link per (from, to) pair and D4's per-source dedup has nothing to merge here. **On a gen 5 x16 link the ceiling would jump to ~504 Gbps**, and the H2D phase at stress-16 would drop to ~30 µs — pulling even N = 32 well under the slot.

![Diagram W — achieved H2D bandwidth on the gNB's superposition call ( perf-fanin-sweep.sh , May 25 2026, on the d3b1a15 build). At N = 1 the per-call transfer is 180 KiB and the link is launch-latency-bound (51 Gbps achieved). As N grows the payload grows linearly (180 KiB × N) and from N = 8 onwards achieved bandwidth asymptotes at ~107 Gbps ≈ 85 % of the 126 Gbps PCIe 5.0 x4 effective ceiling . Per-byte cost flattens; doubling N doubles H2D µs almost exactly. A gen 5 x16 link would lift the ceiling to ~504 Gbps and unlock another ~2× of headroom; the current workstation is capped here. (See the §20.6 prose above for why D4 source-rebuffering doesn't shift this curve on the one-to-N topology.)](../../assets/diagrams/reference-21.svg)

<a href="../../assets/diagrams/reference-21.svg">Open this diagram at full size</a>

Diagram W — achieved H2D bandwidth on the gNB's superposition call (`perf-fanin-sweep.sh`, May 25 2026, on the `d3b1a15` build). At N = 1 the per-call transfer is 180 KiB and the link is launch-latency-bound (51 Gbps achieved). As N grows the payload grows linearly (180 KiB × N) and from N = 8 onwards achieved bandwidth asymptotes at **~107 Gbps ≈ 85 % of the 126 Gbps PCIe 5.0 x4 effective ceiling**. Per-byte cost flattens; doubling N doubles H2D µs almost exactly. A gen 5 x16 link would lift the ceiling to ~504 Gbps and unlock another ~2× of headroom; the current workstation is capped here. (See the §20.6 prose above for why D4 source-rebuffering doesn't shift this curve on the one-to-N topology.)

(perf-fanin)=

### 20.7 What the bench measures — and what it can't

§20.2 noted that the bench is single-threaded and runs each RX node's `process_superposition` call sequentially, so its `p99` is the cost of the *worst single call* rather than the per-slot wall-clock. The 21-config fan-in sweep proves this empirically: every (M, N) configuration collapses onto the `max(M, N)` curve.

![Diagram U — every (M, N) configuration's bench p99 latency collapses onto the 1-N curve when plotted against max(M, N). The bench is single-threaded sequential, so its p99 is the cost of the slowest single process_superposition call — which is the device with the largest fan-in (a gNB with N incoming UL, or a UE with M incoming DL). This is a property of the bench, not the broker. The live broker dispatches per-node calls on parallel CUDA streams (Diagram I) which overlaps the work across the GPU; the per-slot wall-clock cost is ≤ max(M, N) + small overlap residual, not (M + N).](../../assets/diagrams/reference-22.svg)

<a href="../../assets/diagrams/reference-22.svg">Open this diagram at full size</a>

Diagram U — every (M, N) configuration's bench p99 latency collapses onto the 1-N curve when plotted against max(M, N). The bench is single-threaded sequential, so its p99 is the cost of the slowest single `process_superposition` call — which is the device with the largest fan-in (a gNB with N incoming UL, or a UE with M incoming DL). **This is a property of the bench, not the broker.** The live broker dispatches per-node calls on parallel CUDA streams (Diagram I) which overlaps the work across the GPU; the per-slot wall-clock cost is ≤ max(M, N) + small overlap residual, not (M + N).

The bench is therefore an *upper bound* on the live broker's per-slot cost. Confirmed indirectly by the OCUDU smoke run on the 8-edge topology, where the broker measured `h2d ≈ 22 µs · kernel ≈ 5 µs · d2h ≈ 14 µs` per slot — better than the bench's 41 / 6 / 20, because the broker isn't oversubscribing the GPU and gets idle time from the throttle between slots.

(perf-boundary)=

### 20.8 Boundary synthesis on RTX 5090

Pulling everything together — what's realtime, what isn't, and where the wall is:

- **CPU realtime ceiling**: ≈ 2 incoming edges per RX node at 23.04 MS/s with this model chain (yellow at 2 edges, red beyond). Reference path only — never the production target.
- **CUDA bench ceiling (this workstation, gen 5 x4)**: **N ≈ 16 incoming edges** at one RX node is the last yellow point (p99 = 425 µs). At N = 32 the H2D phase alone exceeds the slot. The bench p99 also collapses to `max(M, N)` for M-N topologies (Diagram U), so the same boundary applies to every Set 2 config.
- **CUDA broker ceiling** (estimated): the live broker dispatches per-node calls on parallel CUDA streams (Diagram I) and overlaps them with the throttle's idle time. The OCUDU smoke run at 8 edges measured `h2d ≈ 22 µs · kernel ≈ 5 µs · d2h ≈ 14 µs` per slot in the broker, vs. the bench's 41 / 6 / 20 — confirming the broker isn't oversubscribing the GPU. Extrapolating, the broker on this hardware should handle ~32 incoming edges per RX node before H2D saturation hits.
- **What hits the wall first**: H2D copy latency, growing ~15 µs per staged-buffer edge under the x4 link. Kernel stays ≤ 15 µs even at N = 64 (per-thread inner loop only). D2H is a fixed ~19 µs floor. Compute is wildly underused (56 % SM, 9 % power — see [§20.5](measured-boundaries.md#perf-resource)).
- **Memory is never the constraint** — busiest config uses ~3 MiB per RX node in live working set; total bench footprint under 30 MiB. The 514 MiB device alloc is mostly CUDA runtime context overhead ([§20.5](measured-boundaries.md#perf-resource)).
- **What unlocks more headroom**: **(a) a gen 5 x16 link** — 4× the H2D ceiling, lifts the bench boundary from N ≈ 16 to N ≈ 64 (the GPU has the compute for it, the link is the only thing in the way). **(b) H2D pipelining** (split staged buffer + ping-pong copy) — overlaps copy with the previous slot's kernel; listed in [§26](../../development/designs/scope-history.md#scope) planned work for single-mega-node topologies if such a workload becomes interesting. **(c) Heavier per-edge chains** — adds compute work that the GPU has lots of headroom for, doesn't grow PCIe traffic.
