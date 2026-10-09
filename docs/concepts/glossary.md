# Key terms

(section-2)=

## Key terms — quick reference

The diagrams below name buffers, indices, and struct fields directly from `src/cuda_backend.cu` and `src/broker.cpp`. Skim this table before reading; later sections use these names without re-introducing them.

### Topology

| Term | Meaning |
|----|----|
| node | a device in the graph (a gNB or a UE); the broker serves one RX signal per node, per slot. On a multi-port radio the node is the **radio** and the endpoint pair is a **port** — see [Radio nodes — a radio is not its socket](radio-topology.md#mimo-nodes) |
| port | one ZMQ endpoint pair, its TX ring and its puller. A single-antenna radio has exactly one, and then "port" and "node" name the same thing |
| radio node | one or more ports that are one radio: they share a sample epoch, a throttle, a cursor set and one channel call. Declared under `radio_nodes:`, where **writing order is the matrix index** ([Radio nodes — a radio is not its socket](radio-topology.md#mimo-nodes)) |
| physical link | the directed pair of radio nodes, and the owner of one channel realization: one seed, one absolute-time origin, one correlation factor ([The matrix channel — fixed, correlated, and coherent LOS](channel-models.md#mimo-matrix)) |
| lane | one `(rx_port, tx_port)` coefficient of a physical link, and an ordinary edge to the engine. A physical link expands to `Nt × Nr` lanes |
| Nt / Nr | transmit / receive port counts of a physical link's two radios, taken from the node declarations rather than from any matrix block |
| edge | a directed channel from one node to another, configured under `links:` in the YAML topology with a named `model`. **This doc says "edge" in prose** (a node's "incoming edges" are the edges pointing at it); the YAML key `links:` and the runtime identifiers `link_id` / `link_key` keep the word "link" because they are API names |
| incoming edges | for destination node `d`, the subset of links where `link.to == d`. Resolved once per server thread at broker startup (`src/broker.cpp`); this list is the index space for cursors ([Signal alignment and time discipline](timing.md#alignment)), staged slots ([The staged buffer — packing N edges for one H2D](../reference/backends.md#staged)), per-edge `stage_link()` calls ([The channel — applied per edge (device kernel by default, host fallback)](../reference/device-pipeline.md#kernels)), and the kernel's per-thread edge loop ([Superposition — N edges summed per GPU thread](../reference/device-pipeline.md#superposition)) |
| model | a chain of channel steps (tdl, path_loss, phase, CFO, AWGN) applied per-edge under `models:` in the YAML |
| rx_model | an optional second model applied **once to the summed RX** of a node — typically a thermal-noise floor |
| gNB / UE | 5G base station (gNodeB) / user equipment; both are just "nodes" to the emulator |
| OCUDU | the Split-8 ZMQ SDR runtime that exchanges IQ slots with the broker over `tcp://` |

### Signals and indices

| Term | Meaning |
|----|----|
| IQ sample | one complex baseband sample, an `(i, q)` pair of `float32`s |
| slot / batch | one 1 ms IQ exchange — **23 040 samples** = 23.04 MS/s × 1 ms in the canonical topology (any other sample rate works; batch is derived from `sample_rate_hz × slot_duration`) |
| count | per-batch sample count = 23 040 in the canonical topology (the kernel parameter named `count`) |
| N · link_count | number of incoming edges into a destination node; the per-thread loop bound in `superpose_kernel` |
| k | per-edge loop index inside `superpose_kernel` (0 … N − 1) |
| idx | per-thread / per-sample index (0 … `count` − 1) — every diagram's `idx` is this |
| sequence number | monotonic `uint64_t` tag on every sample written into a source's IQ ring; used by the broker for cursor co-init and common-window reads |

### Buffers (per RX node — CudaSuperposeState )

| Name | What it holds |
|----|----|
| host_staged · device_staged | **The "staged" array.** Every incoming edge's input batch **packed back-to-back** in one contiguous block: edge `k`'s sample `idx` lives at `staged[k·count + idx]`. The broker writes per-edge batches into pinned `host_staged`, then a single H2D copies the whole concatenation to `device_staged` for the kernel to read. Total size: `N × 23 040` IQ samples. |
| host_output · device_output | the node's summed RX signal; the kernel writes `device_output`, a single D2H copies to pinned `host_output`, the broker serves it over ZMQ |
| host_steps · device_steps | flat `GpuStep` array — every edge's chain concatenated head-to-tail |
| host_step_meta · device_step_meta | `2N × int`: the first `N` entries are each edge's offset into `device_steps`; the next `N` are each edge's chain length |
| device_rx_steps | optional second chain (the `rx_model`) — consumed by an in-place `apply_steps_kernel` pass after `superpose_kernel` |

Per-edge state lives in `CudaLinkSlot` (just the `LinkModelState` — the chain steps, Philox seed, and `delay_line` ring). The bench-only per-edge scratch buffers (`host_input · device_input · device_steps · host_output · device_output`) and their lazy-allocation path were unified away with the old `process_into` API; the broker's superposition serve carries all required per-call buffers in `CudaSuperposeState`.

### Chain step ( GpuStep )

| Field | Used by | Meaning |
|----|----|----|
| step.type | all branches | `Scale` \| `Phase` \| `AddNoise` — selects which branch runs in the per-sample chain (the `tdl` chain step is configured but handled before this kernel runs: by `apply_channel_kernel` on device by default, or by host `stage_link()` on fallback) |
| step.a | all | scalar: amplitude factor (Scale — used for path_loss and as the no-op pass-through for a chain-leading tdl), base phase (Phase), noise amplitude (AddNoise) |
| step.b | Phase | phase increment per sample, in radians; `b = 2π·f`<sub>`cfo`</sub>`/f`<sub>`s`</sub> for CFO; `b = 0` for a static phase rotation |
| step.seed · step.counter | AddNoise | Philox4_32_10 state — the RNG is keyed on `(seed, counter + idx)`; `counter` advances by `count` every call |

### Functions, kernels, runtime

| Name | What it is |
|----|----|
| apply_chain() | `__device__` function that walks one chain on (i, q) in registers; called inside both kernels |
| apply_steps_kernel | single-edge kernel; also runs in-place as the `rx_model` pass after `superpose_kernel` |
| superpose_kernel | multi-edge superposition kernel — one thread per output sample, walks every edge, sums into `dst[idx]` |
| stage_link() | host-side fallback helper that runs the chain-leading `tdl` step (multi-tap convolution via `apply_tdl_step{_fading}` in `delay.h`) before the H2D copy. The production path is `apply_channel_kernel` on device ([The channel — applied per edge (device kernel by default, host fallback)](../reference/device-pipeline.md#kernels) Diagram S); `stage_link()` runs only when the per-node dispatch gate doesn't fire (mixed-edge or non-tdl-leading topologies), and as the CPU-backend reference |
| apply_channel_kernel | device-side per-edge channel kernel in `src/device_channel.cu` — runs the multi-tap convolution + Jakes Doppler + Rician LOS on the GPU, materialising the per-tap coarse grid in block shared memory at slot start. Production path for all-leading-tdl topologies; landed Phase 2 D3 |
| process_superposition | the sole CPU / CUDA processor entry point; called by the broker for N-edge fan-in and by the benchmark / tests for the N=1 single-edge case (an earlier `process_into` API was unified away — see [Methodology and test matrices](../reports/performance/measured-boundaries.md#perf-method)) |
| serve | one broker iteration for a destination node: read inputs from each source's ring, stage them, run the kernel, send the result over ZMQ |
| cursor | per-edge `std::atomic<uint64_t>` pointing into its source's IQ ring; co-initialised across edges so superposition aligns every edge's same virtual instant |
| pinned memory | host memory allocated with `cudaHostAlloc`; required for `cudaMemcpyAsync` to actually run async over PCIe DMA |
| Philox4_32_10 | NVIDIA's **counter-based** stateless RNG; same `(seed, counter)` always produces the same draw, so AWGN is bit-reproducible across runs and across thread schedules |

(diagram-rules)=

### Diagram conventions

Every SVG diagram in this document follows the same visual taxonomy. Five semantic classes plus two modifiers; colour and shape encode the semantic class, not the topic. Skim this legend once; later sections use the conventions without re-introducing them.

(leg_b)=

(leg_g)=

(leg_a)=

(leg_n)=
 ![Diagram legend. HOST green = anything host-side (broker thread, host pinned buffer, CPU compute). DEVICE blue = anything device-side (kernel, device global buffer, H2D / D2H copy). DECISION = amber diamond at a branch point. STREAM = amber rectangle for a shared sink (e.g. stdout, results bucket). EXTERNAL = light grey for boundary nodes outside this codebase. The two modifiers layer on top: HIGHLIGHT uses a translucent amber fill to call out a slice of a larger box; FALLBACK / ON-DEMAND uses a dashed border (in the parent class colour) for secondary or optional paths. Arrow colour matches what's flowing: blue for device transport, green for host transport, amber for annotations or highlights, grey for generic data flow. Title text inside a box matches its border colour; body text is light grey; annotations are mid-grey.](../assets/diagrams/reference-01.svg)

<a href="../assets/diagrams/reference-01.svg">Open this diagram at full size</a>

Diagram legend. **HOST** green = anything host-side (broker thread, host pinned buffer, CPU compute). **DEVICE** blue = anything device-side (kernel, device global buffer, H2D / D2H copy). **DECISION** = amber diamond at a branch point. **STREAM** = amber rectangle for a shared sink (e.g. stdout, results bucket). **EXTERNAL** = light grey for boundary nodes outside this codebase. The two modifiers layer on top: **HIGHLIGHT** uses a translucent amber fill to call out a slice of a larger box; **FALLBACK / ON-DEMAND** uses a dashed border (in the parent class colour) for secondary or optional paths. Arrow colour matches what's flowing: blue for device transport, green for host transport, amber for annotations or highlights, grey for generic data flow. Title text inside a box matches its border colour; body text is light grey; annotations are mid-grey.
