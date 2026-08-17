# Progress

**This file is the live-state record for the workspace. Historical progress through 2026-08-16 is preserved in [`archive/progress/AGENT_PROGRESS-2026-08-16.md`](archive/progress/AGENT_PROGRESS-2026-08-16.md).**

## Repository State

- Branch: `main`.
- `main` and `origin/main` are aligned at the documentation publication checkpoint containing the MIMO/Sionna assessment and workspace-maintenance changes.
- The published documentation change set contains the MIMO/Sionna assessment report, the Sionna live-channel plan, removal of the former `docs/superpowers/` documentation tree, the dated progress archive, and this reset live-state file.
- Runtime tests were not rerun because the current changes are documentation and workspace-record maintenance only.

## Workspace Artifacts

- Rank-1 MISO/SIMO and Sionna integration assessment: `docs/mimo-integration-report.html`.
- Sionna live-channel plan: `docs/plans/sionna-live-channel.md`.
- Archived historical progress: `archive/progress/AGENT_PROGRESS-2026-08-16.md`.
- The project source, tests, examples, deployment scripts, and primary technical reference remain in their existing locations.

## Completed Changes

- [Research/MIMO] Produced a source-backed HTML assessment of OCUDU MIMO, the CUDA-accelerated OCUDU fork, srsUE NR limitations, the local project gap, and an initial 2x2 matrix-TDL integration path; the later rank-1 decision below supersedes that initial recommendation.
- [Research/Sionna] Extended the assessment with a source-backed Sionna RT 2.0.1 bridge and added `docs/plans/sionna-live-channel.md`: Sionna remains an out-of-process producer; the proposed broker consumes buffered sparse CIR geometry epochs and complex coefficient horizons by absolute IQ sample; geometry changes may reset delay history while coefficient-only updates preserve it.
- [Plan/Sionna] Defined the parallel MIMO/Sionna dependency graph, bounded v1 multipart record, explicit antenna/link mapping, broker-owned sample timeline, chunk-continuity and epoch-readiness rules, delay-ring bounds, underrun/fallback behavior, record/replay/live modes, S0-S4 phases, and objective validation gates.
- [Validation/Sionna docs] Verified diff whitespace, HTML IDs/anchors/local links, and Markdown local links. Two fresh-reader passes drove fixes for roadmap ordering, current control-batch semantics, startup delay-ring preallocation, wire/link identity, chunk coverage, sample-origin ownership, underrun defaults, S0 pass/fail criteria, navigation, and planned-versus-current wording.
- [Docs/Cleanup] Removed `docs/superpowers/specs/2026-05-26-docker-runtime-design.md` and the now-empty `docs/superpowers/` tree while preserving every document under `docs/plans/`.
- [Workspace Maintenance] Archived the former cumulative progress ledger under `archive/progress/` and reset this file to current live state.
- [Decision/Rank-1] Narrowed the srsUE 5G integration target to OCUDU 2×1/4×1 downlink rank-1 MISO and 1×2/1×4 uplink rank-1 SIMO; revised the HTML report’s endpoint contract, architecture, roadmap, validation gates, risks and claim boundaries, with rank&gt;1 and same-PRB MU-MIMO deferred.
- [Docs/Rank-1 limits] Added a dedicated hard-limits section distinguishing current broker gaps, unverified live DL MISO integration, srsUE PHY limits, one-antenna physical limits, beam/precoder limits, unsupported same-PRB MU-MIMO, the absence of end-to-end 4×4 behavior and srsUE SA restrictions.
- [Docs/Controlling brief] Added a user-owned front section to the HTML report listing the project goal, intended rank-1 experiments, technical requirements, writing/claim requirements and a scope-change rule that governs later report sections.
- [Decision/Sionna export] Made buffered Sionna RT channel-state export part of the controlling brief and approved decision: Sionna produces sparse CIR geometry and coefficient horizons through record/replay then live look-ahead, while the GPU emulator retains real-time IQ timing and processing; added the current implementation gap to the hard-limits table.
- [Review/Sionna integration] Re-reviewed the HTML report and synchronized `docs/plans/sionna-live-channel.md` with the approved rank-1 scope. Added separate FDD DL/UL solves, exact 2×1/1×2 then 4×1/1×4 tensor mappings and IQ equations, ordered endpoint/antenna/polarization manifests, Sionna synthetic-versus-explicit array semantics, baseband coefficient ownership, current 32-tap/128-sample CUDA bounds, coefficient-period/look-ahead sizing, binary ingress lifecycle, and explicit retrace-continuity limits.
- [Validation/Sionna semantics] Checked Sionna RT 2.0.1 primary documentation for `Paths.cir()`/`Paths.taps()` axes and defaults, Numpy export, Doppler time evolution, path validity, `reverse_direction`, and the path solver's default synthetic-array plane-wave approximation. Corrected the oracle claim: Sionna uses an ideal sinc response, while this project uses an 8-tap Hamming-windowed fractional-delay filter, so non-integer delays require declared time/frequency/energy tolerances rather than bit-exact comparison.
- [Git] Published the rank-1 MIMO/Sionna documentation and progress-archive checkpoint directly to `origin/main`.

## Blockers and Risks

- No active blocker is recorded for the completed report and archive work.
- Rank-1 multi-port implementation has not started; the report is an assessment and proposed integration blueprint. The downlink live gate still must verify that the selected OCUDU build supplies a usable fixed rank-1 multi-port PDSCH path without relying on unsupported srsUE PMI feedback.
- Sionna channel-state export is now part of the approved report scope but implementation has not started. S0 is specified; the coefficient-grid/look-ahead defaults, general path-reduction thresholds, attached-run common-delay policy, ingress capacity, FDD scene/material-frequency policy, synthetic-array accuracy gate, and live-mobility input/retrace-transition policy remain explicit pre-S2/S4 decisions.
- Seamless mobility across Sionna scene retraces is not currently supportable: a geometry swap changes delays/path identity and resets affected history. Static geometry plus Doppler is the first live attached-radio target; a fixed-delay-grid or dual-state transition must be designed before claiming continuity across retraces.

## Next Resume Point

- The rank-1 critical path is the multi-port ZMQ transport spike, then deterministic 2×1 downlink-row and 1×2 uplink-column TDL CPU/CUDA paths; extend to four gNB ports only after the two-port gate.
- The approved scalar Sionna S0 static exporter/oracle and S1 record/replay format can proceed in parallel; merge the workstreams at buffered directional-vector CIR ingestion after the deterministic two-port channel exists, then gate live look-ahead mode.
- Freeze the direction/carrier/port-manifest hash and the ideal-sinc-versus-bounded-filter validation metrics before implementing the S1 trace format, because both affect durable records and golden vectors.
