# Mission

**This file defines the long-term mission of the workspace. It is agent-immutable. The agent shall not modify this file autonomously. It may be changed only when the user explicitly and unambiguously instructs a change. Implicit signals, inferred preferences, stylistic adjustments, and routine task updates do not satisfy this condition.**

## Statement

Build and maintain `ocudu-gpu-channel` as a real-time GPU-accelerated wireless-channel emulator for OCUDU's Split 8 ZMQ SDR baseband path, supporting multiple gNBs, multiple UEs, and multiple antenna ports per radio by routing ZMQ IQ sample streams through configurable channel models while preserving sample timing, stream continuity, and OCUDU PHY/RU integration behavior.

## Scope

- Source code, configuration, tests, benchmarks, deployment assets, and documentation for the channel-emulation layer.
- ZMQ IQ sample ingress and egress between OCUDU gNB-side endpoints, UE-side endpoints, brokers, and local test harnesses.
- GPU-accelerated channel processing for one-to-one, one-to-many, many-to-one, and many-to-many gNB/UE topologies.
- Per-link and aggregate channel effects needed to emulate realistic wireless interactions between multiple transmitters and receivers.
- Integration behavior that lets OCUDU Split 8 SDR/ZMQ workflows use the emulator without changing higher-layer OCUDU semantics.
- Grouping several ZMQ endpoints into one logical multi-port radio, and emulating the joint `Nt x Nr` channel matrix between two such radios as one channel realization.

## Non-Goals

- Replacing OCUDU's CU, DU, MAC, scheduler, PHY implementation, 5G core, or non-ZMQ RF drivers.
- Implementing production O-RAN Split 7.2/eCPRI fronthaul or physical RF impairment-hardware control unless the user explicitly expands the mission.
- Building a general-purpose SDR GUI, offline-only signal-analysis tool, or non-real-time simulator detached from ZMQ IQ sample streams.
- Making unsupported claims about OCUDU internals, sample formats, or runtime behavior without verification against public documentation, source code, or runtime tests.
- Claiming live multi-layer (rank > 1) operation on the basis of transport-level multi-port flow alone, or on the basis of several independent single-port UE processes; a live rank-2 claim requires a UE PHY that jointly estimates and decodes the matrix channel.
- Antenna-array geometry, beamforming, precoder selection, and TR 38.901 CDL modelling, unless the user explicitly expands the mission.

## Success Criteria

- The emulator can connect multiple gNB-side and UE-side ZMQ IQ endpoints with a configurable topology.
- Channel models can be configured per link and executed on the GPU with documented CPU fallback or explicit unsupported-path behavior.
- Timing, throughput, and continuity checks demonstrate sustained processing for representative OCUDU ZMQ sample-rate configurations.
- Tests or reproducible harnesses verify endpoint routing, IQ sample handling, channel-effect correctness, and multi-node scenarios.
- Documentation explains how to build, configure, run, and validate the emulator with OCUDU Split 8 ZMQ deployments.
- A multi-port radio pair exchanges IQ through the emulator with every sibling port served from one common sample window, verified against a multi-port test peer, while single-port topologies keep their existing behavior.

## Constraints

- Preserve complex IQ sample ordering, timing, and stream-continuity semantics expected by OCUDU-facing ZMQ SDR paths.
- Treat multi-gNB and multi-UE operation as a first-class design requirement, not a later extension of a single-link-only architecture.
- All ports of one radio share one sample epoch, and all matrix coefficients between two radios belong to one channel realization with one time origin; per-port state that can drift independently is not an acceptable implementation of a matrix channel.
- Keep real-time data paths measurable and bounded in allocation, buffering, and latency.
- Make GPU availability, device selection, fallback behavior, and unsupported hardware conditions explicit at runtime.

---

**Amendment procedure.** A change to any section of this file requires an explicit user instruction naming the section and the new content. The agent shall record no amendment in `AGENT_PROGRESS.md` beyond a single bullet noting that the user amended the mission. The agent shall not treat a new task, a new workstream, or a shift in emphasis as implicit amendment authority.
