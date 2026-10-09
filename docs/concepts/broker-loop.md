# Broker processing loop

(section-8)=

(broker-loop)=

## The broker — per-slot loop

**Reader note.** This section names machinery covered in detail in §9 (alignment), §10 (staging), §5 (channel models), and §11 (kernels and pipeline, with op-order in §11.0). The §2 glossary defines every term used here as a one-liner, so a first read can proceed top-to-bottom; the deeper "why" for each named component lives in the cross-referenced section.

**Thread structure, stated once.** The per-slot work of a destination node is split across two threads rather than one: a **producer** per radio node chooses the window, runs the pipeline and publishes each output row, and a thin **REP worker** per port owns only the socket state machine. The split is what makes a multi-port radio's ports share one window by construction, and its reasoning is in [§23](timing.md#mimo-epoch). For a single-port radio the two threads do between them exactly what the single server thread described below did, in the same order, so this section reads correctly either way — "the server thread" below means "the producer, then its REP worker".

The broker runs one **producer per destination node**. It reads the common window of samples available across all of the node's incoming edges, stages them into pinned memory, runs the GPU pipeline, and hands the processed IQ to the node's REP worker(s), which reply to the downstream radio's requests. Two producers for the same node never coexist; producers for different nodes run in parallel and use independent CUDA streams (see [§16](../reference/backends.md#stream-concurrency)).

Receive-side rings are written by a separate set of RX threads — one per source — that pull TX packets from each source's PUB socket and append samples (with a monotonic sequence number) to a per-source ring buffer. The server thread only reads from these rings; it never writes to them.

**Node ↔ edge mapping (the per-server-thread invariant).** The YAML topology is two flat lists — `devices:` (nodes) and `links:` (directed edges with `from` / `to` / `model`). When the broker spawns the server thread for destination device `d`, it resolves that node's **incoming-edges set** by filtering the global links once at startup: `incoming = { i : links[i].to == d }` ([`src/broker.cpp`](https://github.com/zhouyou-gu/ocudu-gpu-channel/blob/main/src/broker.cpp):420-425). From that list it builds a fixed per-thread `std::vector<SuperpositionInput> superposition`, where each entry holds the edge's `link_key` (the processor's state map key) and a pointer to its `model`. That vector is then the index space for **everything the server does per slot** — one cursor per `incoming[k]` in alignment co-init (§9), one ring read per `incoming[k]` in the common-window read, one `stage_link()` call per `incoming[k]` to apply that link's `h`<sub>`(src→dst)`</sub>`(t, τ)` into staged slot `k` (§11), and the kernel's per-thread inner loop walks `k = 0 … incoming.size()−1` to sum across edges (§12). The mapping is computed once at broker startup and is fixed for the run; the per-slot loop never re-resolves it.

(arv)=
 ![Diagram V — node ↔ edge mapping by execution side, post Phase 2. Top row (build): at broker startup, Broker::prepare() filters the global links\[\] once per destination ( incoming = { i : links\[i\].to == d.id } ) and builds a fixed per-server-thread vector superposition\[k\] = { link_key, model\* } . This entire resolution lives on the host. Bottom row (use): every slot the server thread walks k = 0 … N−1 on superposition\[k\] to drive cursor co-init, ring read, and the pack into host_source_iq . D4 source rebuffering : the pack writes one slot per UNIQUE source (not per edge) — edges sharing a source share one slot. The per-edge channel math ( apply_tdl_step_fading from delay.h ) runs on the GPU as apply_channel_kernel by default; host stage_link() only runs as the fallback when a destination has any non-tdl-leading incoming edge. The device kernel never sees superposition\[\] directly — it sees the packed result ( device_source_iq\[s · count + idx\] indexed per edge via DeviceLinkState::src_index , plus per-edge DeviceLinkState + step_meta\[2N\] ) built host-side from superposition\[\] and shipped via H2D. H2D bytes per slot scale with source count , not edge count.](../assets/diagrams/reference-04.svg)

Diagram V — node ↔ edge mapping by execution side, post Phase 2. **Top row (build):** at broker startup, `Broker::prepare()` filters the global `links[]` once per destination (`incoming = { i : links[i].to == d.id }`) and builds a fixed per-server-thread vector `superposition[k] = { link_key, model* }`. This entire resolution lives on the host. **Bottom row (use):** every slot the server thread walks `k = 0 … N−1` on `superposition[k]` to drive cursor co-init, ring read, and the pack into `host_source_iq`. **D4 source rebuffering**: the pack writes one slot per UNIQUE source (not per edge) — edges sharing a source share one slot. The per-edge channel math (`apply_tdl_step_fading` from `delay.h`) runs on the GPU as `apply_channel_kernel` by default; host `stage_link()` only runs as the fallback when a destination has any non-tdl-leading incoming edge. The device kernel never sees `superposition[]` directly — it sees the packed result (`device_source_iq[s · count + idx]` indexed per edge via `DeviceLinkState::src_index`, plus per-edge `DeviceLinkState` + `step_meta[2N]`) built host-side from `superposition[]` and shipped via H2D. H2D bytes per slot scale with **source count**, not edge count.

The per-serve sequence in `src/broker.cpp` is a strict pipeline — **align → read → delay → stage → kernel → return → throttle → advance**. Each later stage assumes the guarantee the previous one made. The shape, laid out by resource (which thread / which device does the work) reads like this:

```{raw} html
<div class="legacy-diagram"><div class="bk-pipe">

<div class="bk-axis">
<div class="bk-axis-spacer"></div>
<div class="bk-axis-bar">
<span class="bk-tick" style="left:0%;"></span>
<span class="bk-tick-label" style="left:1%;">t = 0<span class="bk-tick-sub">REP received</span></span>
<span class="bk-tick" style="left:44%;"></span>
<span class="bk-tick-label" style="left:44%;">GPU starts<span class="bk-tick-sub">H2D launch</span></span>
<span class="bk-tick" style="left:70%;"></span>
<span class="bk-tick-label" style="left:70%;">GPU done<span class="bk-tick-sub">D2H complete</span></span>
<span class="bk-tick" style="left:100%;"></span>
<span class="bk-tick-label" style="left:99%;">t = slot deadline<span class="bk-tick-sub">500 µs / 1 ms (SCS)</span></span>
</div>
</div>

<div class="bk-lane">
<div class="bk-label">ZMQ socket<small>per node</small></div>
<div class="bk-track">
<div class="bk-stage bk-stage--zmq" style="left:0%; width:9%;">
<b>1. wait REP</b><small>zmq_recv</small>
</div>
<div class="bk-stage bk-stage--zmq" style="left:81%; width:9%;">
<b>7b. REP send</b><small>zmq_send reply</small>
</div>
</div>
</div>

<div class="bk-handoff-layer"><div class="bk-handoff-row">
<div class="bk-handoff-label">ZMQ → CPU</div>
<div class="bk-handoff-arrows">
<span class="bk-handoff" style="left:9.5%;">↓</span>
</div>
</div></div>

<div class="bk-lane">
<div class="bk-label">host CPU<small>broker thread</small></div>
<div class="bk-track">
<div class="bk-stage bk-stage--cpu" style="left:10%; width:8%;">
<b>2. align</b><small>§9 cursors</small>
</div>
<div class="bk-stage bk-stage--cpu" style="left:18.5%; width:7%;">
<b>3. read</b><small>rings</small>
</div>
<div class="bk-stage bk-stage--cpu" style="left:26%; width:8%;">
<b>4. pack</b><small>raw IQ → pinned</small>
</div>
<div class="bk-stage bk-stage--cpu" style="left:34.5%; width:8%;">
<b>5. step_meta</b><small>§10 build</small>
</div>
<div class="bk-stage bk-stage--cpu" style="left:71%; width:9%;">
<b>7a. throttle</b><small>sleep_until</small>
</div>
<div class="bk-stage bk-stage--cpu" style="left:91%; width:9%;">
<b>8. advance</b><small>cursors += serve</small>
</div>
</div>
</div>

<div class="bk-handoff-layer"><div class="bk-handoff-row">
<div class="bk-handoff-label">CPU ↔ GPU ↔ ZMQ ↔ CPU</div>
<div class="bk-handoff-arrows">
<span class="bk-handoff" style="left:43%;">↓</span>
<span class="bk-handoff" style="left:70.5%;">↑</span>
<span class="bk-handoff" style="left:80.5%;">↓</span>
<span class="bk-handoff" style="left:90.5%;">↑</span>
</div>
</div></div>

<div class="bk-lane">
<div class="bk-label">GPU device<small>per-node CUDA stream</small></div>
<div class="bk-track">
<div class="bk-stage bk-stage--gpu" style="left:43.5%; width:6%;">
<b>6a. H2D</b><small>raw → device</small>
</div>
<div class="bk-stage bk-stage--gpu" style="left:49.7%; width:7.5%;">
<b>6b. channel</b><small>apply_channel_kernel · §11</small>
</div>
<div class="bk-stage bk-stage--gpu" style="left:57.4%; width:6%;">
<b>6c. superpose</b><small>§12</small>
</div>
<div class="bk-stage bk-stage--gpu" style="left:63.6%; width:6.5%;">
<b>6d. D2H</b><small>output → host</small>
</div>
</div>
</div>
<div class="bk-legend">
<span><span class="bk-legend-sw" style="background:rgba(205,214,230,0.30); border:1px solid #cdd6e6;"></span>ZMQ (EXTERNAL)</span>
<span><span class="bk-legend-sw" style="background:rgba(111,208,140,0.45); border:1px solid #6fd08c;"></span>host CPU work (HOST)</span>
<span><span class="bk-legend-sw" style="background:rgba(93,177,255,0.45); border:1px solid #5db1ff;"></span>GPU device work (DEVICE)</span>
<span>↓↑ = hand-off (resource transfer)</span>
</div>
<div class="bk-disclaimer">
      Bar widths are nominal — they show <b>strict-sequential ordering</b> and resource
      ownership, not measured timings. In practice stages 1–6 fit in ~10–20 µs and the
      throttle (7a) absorbs whatever's left of the 1 ms slot.
    </div>
</div></div>
```

Diagram K — the per-serve pipeline laid out by resource (ZMQ socket / host CPU / GPU device) on the Phase 2 device-default path. Each stage strictly blocks the next; the arrows between lanes mark hand-offs where ownership transfers (ZMQ→CPU after the request arrives, CPU→GPU after the pack into `host_source_iq`, GPU→CPU after D2H, CPU→ZMQ after the throttle anchor hits). The per-edge channel work that used to live in CPU stage 4 (`stage_link`) now runs on the GPU as sub-phase 6b (`apply_channel_kernel` — multi-tap convolution + Jakes + Rician LOS); the host CPU lane is now mostly just packing + step_meta build. **Host fallback path**: if any incoming edge is non-tdl-leading, the dispatch gate goes false and stage 4 reverts to host `stage_link` writing the shaped IQ into `host_staged`, with 6b collapsed back to just the copy — see [§11 Diagram S](../reference/device-pipeline.md#kernels) for the gate. Section refs (§) link to the dedicated sections.

Detail per stage:

1.  **Wait for REP request** on the node's socket.
2.  **Align** — first serve: co-init every incoming-edge cursor to its source ring's `earliest_sequence()` so all edges share an epoch (sequence 0). Subsequent serves: cursors are already aligned because they only ever advance together (step 8). **Compute the common window** `serve = min(batch, min over k of (next_seq − cursor))` — the largest slice every edge can supply. If the window is zero the thread polls again, and `rx_starvations` increments if the wait exceeds the starvation deadline. **Detail in [§9](timing.md#alignment) + Diagram H.**
3.  **Read** the common window from each source ring at its cursor — N separate per-edge sample arrays, each `serve` samples long, all representing the same source-time slice.
4.  **Apply per-edge leading propagation** — by default on the device kernel `apply_channel_kernel` (Phase 2 D3 + D4): raw IQ is packed per UNIQUE source into `host_source_iq` (D4 source rebuffering — N edges sharing a source share one slot, indexed by `DeviceLinkState::src_index`), ships H2D into `device_source_iq`, the kernel materialises per-tap Jakes coarse grids in block shared memory and writes shaped IQ into `device_staged`. Host fallback via `stage_link()` runs the same math in `delay.h` when the per-node dispatch gate doesn't fire (mixed-edge or non-tdl-leading nodes). Edges without a leading tdl just get a plain copy. **Detail in [§10](../reference/backends.md#staged) top prose, [§5](../reference/configuration.md#channels) tdl row, and [§11 Diagram S](../reference/device-pipeline.md#kernels).**
5.  **Stage** — only used on the host-fallback path: pack the N per-edge arrays back-to-back into the pinned `host_staged` buffer for one H2D copy. The device-default path skips this entirely (it uses `host_source_iq` directly). **Detail in [§10](../reference/backends.md#staged) + Diagram J.**
6.  **Kernel** — `process_superposition` runs the H2D, launches `superpose_kernel` (+ optional in-place `rx_model` pass), and does the D2H. **Detail in [§§11–13](../reference/device-pipeline.md#kernels).**
7.  **Throttle then ZMQ REP send** — sleep until `throttle_anchor + served / rate` (so wall-clock matches virtual time), then reply with the processed IQ.
8.  **Advance cursors** by `serve` samples on every edge — keeps them aligned for the next serve.
