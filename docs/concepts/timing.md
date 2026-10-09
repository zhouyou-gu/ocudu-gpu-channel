# Timing and sample alignment

(section-9)=

(alignment)=

## Signal alignment and time discipline

Three layers of alignment compose to make the emulator behave physically:

1.  **Sequence-aligned superposition** — every incoming-edge cursor reads from the same virtual time slice on every serve, so the kernel's `staged[k·count + idx]` for different `k` always represents the same instant in source time.
2.  **Per-edge propagation delay** — a chain-leading `tdl` step shifts an edge back in time (and optionally splits it across multipath taps), with continuity preserved across slot boundaries via a per-edge `delay_line` ring (in `DeviceLinkState` global memory on the device path, in `LinkModelState` on the host fallback).
3.  **Wall-clock throttle** — the producer sleeps to pace serves at exactly the sample rate, so virtual time = real time. It paces to that rate and no faster, and it will not make up time it lost while blocked: a lock-step radio has no clock of its own, so the emulator is its clock, and a burst of catch-up is what a multi-port ZMQ radio dead-locks on rather than absorbs ([The broker is the radio's clock, and must not run fast](timing.md#mimo-pacing)).
4.  **One epoch per radio** — on a multi-port radio, all ports advance on the same window, chosen once by one thread and cut on the same boundaries all the way to the wire ([One sample epoch per radio](timing.md#mimo-epoch)). Per-port state that can drift independently is not an acceptable implementation of a matrix channel.

![Diagram H — per-source rings + co-initialised cursors + a common-window read. Co-init gives every edge a shared time origin; the common-window rule keeps them aligned every serve. (Wall-clock pacing is visualised separately in Diagram L; per-edge delay in Diagram M.)](../assets/diagrams/reference-05.svg)

<a href="../assets/diagrams/reference-05.svg">Open this diagram at full size</a>

Diagram H — per-source rings + co-initialised cursors + a common-window read. Co-init gives every edge a shared time origin; the common-window rule keeps them aligned every serve. (Wall-clock pacing is visualised separately in Diagram L; per-edge delay in Diagram M.)

Diagram H is one moment — a single serve. Diagram L shows the same cursors evolving across many serves: every cursor advances by `serve` samples per step, and a wall-clock throttle pins each serve boundary to real time so virtual time tracks real time for the life of the run.

![Diagram L — cursor evolution across serves. Diagram H showed one moment; this shows the whole sequence. Every source's cursor starts at sequence 0 (co-init at t = 0 ) and advances by serve samples each serve, in lockstep across all edges. The wall-clock throttle pins each serve boundary to real time, so virtual time = real time for the life of the run.](../assets/diagrams/reference-06.svg)

<a href="../assets/diagrams/reference-06.svg">Open this diagram at full size</a>

Diagram L — cursor evolution across serves. Diagram H showed one moment; this shows the whole sequence. Every source's cursor starts at sequence 0 (co-init at `t = 0`) and advances by `serve` samples each serve, in lockstep across all edges. The wall-clock throttle pins each serve boundary to real time, so virtual time = real time for the life of the run.

Diagrams H and L describe alignment *between* sources — the same virtual time index reaches every ring at the same moment. Diagram M zooms in on what happens *after* alignment: each per-edge leading `tdl` step shifts its source's contribution by an independent per-edge delay `d`<sub>`k`</sub>, so the same virtual instant arrives at different points along each edge's tap timeline.

![Diagram M — the same virtual time index idx reaches different source-sequence positions on different edges, because each edge applies its own leading tdl with delay d k (for the single-tap case the diagram illustrates; multi-tap produces several echoes at d k , d k +τ₁, … in parallel). At idx = 7 the receiver integrates src 0 's sample 5 (= 7 − d 0 ) and src 1 's sample 2 (= 7 − d 1 ). d k composes propagation distance and constant TX offset into one per-edge knob — the emulator cannot distinguish them.](../assets/diagrams/reference-07.svg)

<a href="../assets/diagrams/reference-07.svg">Open this diagram at full size</a>

Diagram M — the same virtual time index `idx` reaches different source-sequence positions on different edges, because each edge applies its own leading `tdl` with delay d<sub>k</sub> (for the single-tap case the diagram illustrates; multi-tap produces several echoes at d<sub>k</sub>, d<sub>k</sub>+τ₁, … in parallel). At idx = 7 the receiver integrates `src 0`'s sample 5 (= 7 − d<sub>0</sub>) and `src 1`'s sample 2 (= 7 − d<sub>1</sub>). d<sub>k</sub> composes propagation distance and constant TX offset into one per-edge knob — the emulator cannot distinguish them.

**Layer 2 in detail — what d<sub>k</sub> bundles together.** d<sub>k</sub> in the diagram above is the chain-leading sample-delay step on the edge, and it absorbs two physically distinct effects. The receiver can't tell them apart, but the YAML exposes a named knob for each so a topology can express each effect cleanly:

- **Propagation delay** — a per-edge, geometry-driven physical propagation time. A source farther from the receiver in the topology corresponds to a larger contribution. One sample at the OCUDU / srsRAN sample rate (≈ 23.04 MS/s) is ~13 m of free-space propagation, so a 100 m edge is roughly 8 samples. YAML knob: `propagation_delay_samples` on the **edge**.
- **TX timing offset** — a per-source, constant TX-start-time lag. If a radio brought its ZMQ TX socket up later than the others, every sample it produces is shifted in virtual time by exactly that constant. Indistinguishable from being farther away by the same number of samples, so the broker folds it into the same chain-leading delay step. YAML knob: `tx_timing_offset_samples` on the **device** — applies uniformly to every outgoing edge.

At load time, `fold_link_leading_delays()` composes these two knobs with whatever leading `tdl` step the edge's model already has: if the chain leads with a `tdl`, every tap's `delay_samples` is shifted by the summed offset (preserving multi- tap multipath); otherwise a single-tap `tdl` is prepended with the offset as its tap delay. d<sub>k</sub> for an edge is the sum of all three sources — see [Channel models in YAML](../reference/configuration.md#channels) for the YAML recipes.

**What is not modeled at this layer:** any time-varying offset (clock drift, slot misalignment that grows over time, sample-rate mismatch between endpoints) is out of scope. d<sub>k</sub> is constant for the life of a serve and changes only when the YAML is reloaded — drift would require a runtime-mutable per-edge delay, which is on the planned list in [Scope and planned work](../development/designs/scope-history.md#scope).

**What this stage guarantees for everything downstream:** after this step, the broker holds `N` per-edge sample arrays that are **(a)** all exactly `serve` samples long, and **(b)** whose element at index `idx` represents the same source-time instant on every edge. The next stage ([The staged buffer — packing N edges for one H2D](../reference/backends.md#staged), staging) takes these aligned arrays as given and concatenates them; the kernel ([The channel — applied per edge (device kernel by default, host fallback)](../reference/device-pipeline.md#kernels)) takes the staged buffer as given and sums `staged[k·count + idx]` across edges to produce the node's RX sample at instant `idx`.

(section-23)=

(mimo-epoch)=

## One sample epoch per radio

A matrix channel is a statement about simultaneity: every coefficient of `H` relates signals from **one instant**. Two sockets that advance independently do not describe a matrix channel no matter how correct the arithmetic is. This section is how the broker makes that unrepresentable rather than merely unlikely.

The per-slot data path splits into three thread roles, `nodes + 2 × ports` in total:

- **A puller per port** drains its peer's TX into that port's ring. Unchanged from Parts III–IV.
- **A producer per radio node** chooses the serve window **once**, reads every incoming lane at that window, calls the channel exactly once, advances every source cursor by the same count in one loop, paces to the node sample rate, and pushes each output row into its RX port's ring.
- **A REP worker per port** owns only the socket state machine: receive a request, pop one window, reply. It selects nothing.

The window choice moving out of the request-driven thread is the whole point. In the single-antenna design each destination's server thread picked its own window at its own instant; with two such threads owning the two rows of one radio, they could pick different counts and drift apart permanently. With one producer per node there is no second thread that *could* pick a different window, so sibling ports consuming different sample ranges is not a state the design can reach.

That guarantee has to survive the last hop to the wire. The producer publishes a window to every sibling ring in one loop; the REP workers then answer **with exactly one such window**, taken from a queue of boundaries the producer wrote under the same lock as the samples. Sizing a reply the obvious way instead — `min(batch, available)` — reads ring occupancy, and two sibling threads sample that at two different instants, so a producer push landing between them splits one common window into two differently-sized replies. That is not a hypothetical: it happened, at a rate of 4 groups in 23 483, and it is what the two-port test peer's `sibling_size_mismatches` counter exists to see.

Nothing zero-fills. An empty ring means the REP worker holds the request until real processed IQ exists, exactly as the single-antenna server did, which is also how OCUDU's own gNB TX behaves when its buffer is empty.

(mimo-pacing)=

### The broker is the radio's clock, and must not run fast

A ZMQ radio has no clock of its own. It advances only as fast as the broker exchanges samples with it, which makes the broker its time base — and a time base that runs *fast* is not a harmless inefficiency on a multi-port radio. It is a deadlock, and the mechanism is worth stating because it is invisible from our side of the socket:

- OCUDU's ZMQ RX channel pushes each received message into a fixed circular buffer and, when that buffer is full, **retries in place** with a 1 µs sleep (`radio_zmq_rx_channel.cpp`).
- That retry runs on the single `radio` worker thread that serves **every** TX and RX channel of the session (`worker_manager.cpp`, `create_prio_worker` → `single_worker`).
- The RX stream pops its ports **in sequence**. So one full port captures the thread its starved sibling needs, the PHY blocks on the sibling, and the full port is never drained.

On a single-port radio that same loop is self-clearing back pressure — the PHY keeps popping the one buffer being pushed. From two ports up it is a hard deadlock, in both directions at once, at 0% CPU. The emulator must therefore never hand a radio samples faster than its nominal rate, and "never" includes recovering from its own idle time: a pacer that sleeps until `anchor + produced/rate` is not a rate limiter while it is behind. Every second the producer spent blocked waiting for the radio to start requesting becomes a second's worth of slots it is owed, and it emits them back to back. The pacer therefore **drops lateness beyond one batch** rather than spending it (`include/ocudu_gpu_channel/pacing.h`). Under-feeding is safe — a lock-step radio simply waits — and over-feeding is not.
