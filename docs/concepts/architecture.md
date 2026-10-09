# Architecture and data flow

(section-3)=

## System architecture — three layers

Zooming out before zooming in: every IQ sample makes a round-trip through three layers per slot. The external SDR layer is the user's existing software stack; the broker is this project; the GPU executes channel math on the primary device path. Static mixed models can use host staging, and the CPU reference follows the separate backend path described in [backends](../reference/backends.md).

```{raw} html
<div class="legacy-diagram"><div class="tier-stack">
<div class="tier tier--host">
<div class="tier-label">External SDR layer · live radio stacks</div>
<div class="tier-cells">
<div class="tier-cell">srsRAN gNB <small>5G base station</small></div>
<div class="tier-cell">OCUDU runtime <small>Split-8 ZMQ SDR</small></div>
<div class="tier-cell">srsRAN srsUE <small>user equipment</small></div>
</div>
</div>
<div class="flow-arrow">ZMQ <code>tcp://</code>  ·  cf32 IQ in 23 040-sample / 1 ms slots</div>
<div class="tier tier--broker">
<div class="tier-label">ocudu-gpu-channel broker · this project</div>
<div class="tier-cells">
<div class="tier-cell">RX rings + cursors <small>per source, sequence-tagged</small></div>
<div class="tier-cell">Per-node server threads <small>one ZMQ REP socket each</small></div>
<div class="tier-cell">YAML topology + models <small>nodes, links, channels</small></div>
</div>
</div>
<div class="flow-arrow">PCIe DMA  ·  <code>cudaMemcpyAsync</code> H2D / D2H  ·  per-node <code>cudaStream_t</code></div>
<div class="tier tier--gpu">
<div class="tier-label">GPU · CUDA backend</div>
<div class="tier-cells">
<div class="tier-cell">superpose_kernel <small>per-thread loop over N edges</small></div>
<div class="tier-cell">apply_steps_kernel <small>single-edge + in-place rx_model</small></div>
<div class="tier-cell">apply_chain() <small>device fn: Scale / Phase / AddNoise</small></div>
</div>
</div>
</div></div>
```

Diagram F — three layers, one direction per slot. IQ arrives over ZMQ, stages into pinned host buffers, copies to the GPU, processes, copies back, and is served to the next ZMQ REP request.

(section-7)=

(master-flow)=

## End-to-end serve flow — one slot at a glance

Diagram F ([System architecture — three layers](architecture.md#section-3)) showed the three layers; this section is the single most-useful diagram in the doc — one destination node's serve, start-to-finish. Every later section is a deep-dive into one box of this picture.

(arwmf)=
 ![Diagram MF — the canonical four-step serve. Every destination node loops this independently on its own thread + CUDA stream ( §16 Diagram I ). Deep dives: step 2 (align/read/pack) in §9 + §10 ; step 3 GPU pipeline in §11 (Diagram S) + §11.0 (op order) + §15 (memory map); step 4 throttle in §9 .](../assets/diagrams/reference-03.svg)

<a href="../assets/diagrams/reference-03.svg">Open this diagram at full size</a>

Diagram MF — the canonical four-step serve. Every destination node loops this independently on its own thread + CUDA stream ([Multi-stream concurrency — overlapping serves](../reference/backends.md#stream-concurrency)). Deep dives: step 2 (align/read/pack) in [Signal alignment and time discipline](timing.md#alignment) + [The staged buffer — packing N edges for one H2D](../reference/backends.md#staged); step 3 GPU pipeline in [The channel — applied per edge (device kernel by default, host fallback)](../reference/device-pipeline.md#kernels) (Diagram S) + [Pipeline op order — what each stream actually launches](../reference/device-pipeline.md#pipeline) (op order) + [Signal memory — pinned host buffers paired with device buffers](../reference/backends.md#signal-memory) (memory map); step 4 throttle in [Signal alignment and time discipline](timing.md#alignment).

(part-iii)=
