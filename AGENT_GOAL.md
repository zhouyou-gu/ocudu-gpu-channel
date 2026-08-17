# Mission

**This file defines the long-term mission of the workspace. It is agent-immutable. The agent shall not modify this file autonomously. It may be changed only when the user explicitly and unambiguously instructs a change. Implicit signals, inferred preferences, stylistic adjustments, and routine task updates do not satisfy this condition.**

This workspace (`ocudu-gpu-channel-rank1`) was forked from `ocudu-gpu-channel-mimo-claude` on 2026-08-17 by explicit user instruction. Its mission is the rank-1 MISO/SIMO objective of the supervisor's integration assessment (`mimo-integration-report.html`), with every Sionna RT item excluded: Sionna integration is owned by another team member and will be merged later. The rank-2 OAI-nrUE workstream continues in the parent workspace and is out of scope here.

## Statement

Build and maintain this fork of `ocudu-gpu-channel` as a real-time GPU-accelerated wireless-channel emulator that demonstrates useful rank-1 multi-antenna operation between a multi-port OCUDU gNB and a one-port srsUE 5G: downlink 2×1 (later 4×1) MISO where multiple gNB transmit ports combine through the emulated channel into srsUE's single receive stream, and uplink 1×2 (later 1×4) SIMO where srsUE's single transmit stream reaches multiple gNB receive ports over independently modelled branches, while preserving sample timing, stream continuity, and OCUDU PHY/RU integration behavior.

## Scope

- Source code, configuration, tests, benchmarks, and documentation for rank-1 asymmetric (MISO/SIMO) channel emulation in this fork.
- ZMQ IQ ingress/egress between a multi-port OCUDU gNB radio node and a single-port srsUE radio node, plus synthetic test peers.
- Downlink 1×N_TX row-vector channels (one effective stream at the UE) and uplink N_RX×1 column-vector channels (one independently modelled branch per gNB receive port), executed on the GPU with a CPU reference.
- Keeping srsUE at `nof_antennas = 1`; starting OCUDU at `nof_antennas_dl: 2` / `nof_antennas_ul: 2`; extending the gNB side to four ports only after the two-port gates pass.
- Evidence-backed end-to-end results: attach, PDU session, traffic, PHY metrics, and channel telemetry, with per-branch gain/delay/fading isolation proved against synthetic peers before live srsUE runs.

## Non-Goals

- Rank > 1 SU-MIMO, multi-layer UE decode, or any UE-side rank claim: srsUE remains a one-transmit-stream, one-receive-stream, one-layer NR UE throughout. (The rank-2 workstream lives in the parent workspace.)
- UE receive diversity, UE receive-beam steering, or representing the UE as more than one antenna.
- Automatic closed-loop PMI-driven downlink beam control: srsUE's one-port CSI cannot drive it; any beam weight is fixed or explicitly oracle-labelled and lives in OCUDU/RU, not in this emulator.
- Same-PRB MU-MIMO, user grouping, or multi-user precoding.
- Calling duplicated downlink samples "diversity" without verified precoding or a supported diversity mode.
- Sionna RT integration in any form (export, record/replay, live CIR): another team member owns it; this fork only avoids design choices that would block a later merge.
- Replacing OCUDU's CU/DU/MAC/scheduler/PHY, the 5G core, or non-ZMQ RF drivers; patching srsUE itself.

## Success Criteria

- A 2-port OCUDU gNB radio node and a 1-port srsUE radio node exchange IQ through the emulator with every gNB sibling port served from one common sample window, verified against synthetic peers with all data-integrity counters at zero.
- Deterministic 2×1 DL and 1×2 UL coefficient vectors are proved before live runs: per-branch isolation (antenna-0-only and antenna-1-only reach the UE with expected gain/delay), coherent DL phase sweeps show expected addition and cancellation, UL branches preserve independent gain/delay/fading/noise, and CPU/CUDA agree within established tolerance.
- Live srsUE completes attach, PDU session, and traffic through the 2×1/1×2 emulated channel with zero queue overflows, sequence gaps, and ZMQ errors, and the served IQ is verified against the declared channel vectors on the wire.
- The 4×1/1×4 extension repeats the same correctness, timing, and real-time gates without changing the one-port UE contract.
- Every result is labelled with hardware, sample rate, topology, model chain, backend, and run duration, and is described as rank-1 MISO/SIMO — never as end-to-end MIMO or UE spatial multiplexing.

## Constraints

- All ports of the gNB radio node share one sample epoch; a directional coefficient vector activates atomically at one slot boundary — a partially updated port vector is not an acceptable state.
- Precoding and beam weights belong to OCUDU/RU; propagation, fading, delay, noise, and superposition belong to this project. Receiver noise is applied once per receiver, not per propagation coefficient.
- The existing 1×1 OCUDU↔srsUE attach gate is preserved unchanged as the regression net.
- Keep real-time data paths measurable and bounded in allocation, buffering, and latency; never zero-fill missing channel state silently.
- Make GPU availability, device selection, fallback behavior, and unsupported hardware conditions explicit at runtime.

---

**Amendment procedure.** A change to any section of this file requires an explicit user instruction naming the section and the new content. The agent shall record no amendment in `AGENT_PROGRESS.md` beyond a single bullet noting that the user amended the mission. The agent shall not treat a new task, a new workstream, or a shift in emphasis as implicit amendment authority.
