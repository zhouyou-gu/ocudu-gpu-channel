# Progress

> **Entry rules:** One bullet per meaningful change; at most two sentences and 40 words. Record outcomes, decisive checks and blockers; link details and omit repeated context. Apply the [harness rules](AGENT_HARNESS.md#progress-entries) before finishing.

## Current Objective

Performance-report PRs #9 and #10 are merged, contributor history is preserved, and local main is synchronized. The documentation fix passes RTX validation and GitHub CI.

## Repository State

- Local presentation artifacts remain in ignored `slides/`; the directory is also excluded from uploads and Docker builds. Vocabulary review is complete, with 80 definitions and six unresolved wording or protected-name records.

- README contributor credits include Quanlong Zhao; Markdown rendering and whitespace checks passed on RTX.

- Comprehensive RTX validation is complete; launcher fixes are committed as `fb07a67` and dashboard corrections as `2b31656`. Five live configurations passed connectivity; timing misses remain; see the [report](docs/reports/validation/full-validation-20261009.md).

- Current files and the generated site are sanitized; measurements and runtime behavior are preserved. Cleanup is committed as `b02687a`; existing commits retain their original content (`validation/text-redaction-20261009/`).

- Whole-project cleanup is validated and committed: obsolete source stubs and generated clutter removed, current guidance repaired, upload exclusions shared, and unused diagram styles trimmed. Evidence: `validation/cleanup-20261009/`.

- Root milestone cleanup is validated and committed: six forwarding files removed, current links updated, and full historical records retained.

- Documentation migration is complete in five stages: Markdown site, current guidance, reports/history, compatibility routing and RTX validation. The structure and documentation commits are published on `origin/main`.

- Approved structure migration is published: `use_cases/`, runtime apps, grouped integrations/tests, Python packaging and documentation navigation. Validation evidence is in `validation/structure-20261009/`.

- Checkout: `main` in `${HOME}/Documents/GitHub/ocudu-gpu-channel`. PRs #2–#6, #9 and #10 are integrated through normal merges; Git records the current commit and publication state.
- Integration and cleanup were published on 2026-09-14. Normal merges `22a51ba` and `e59e095` retain contributor tips `066a702` and `e31fe51`; no history rewrite or release tag.
- Only this channel worktree and local `main` remain. Origin branch cleanup was verified on 2026-09-14; contributor remotes `ehs0` and `fork` remain.
- Agent-file updates: concise harness/progress, full historical archive, short-entry rules and the user-requested mission reset. Pre-existing runtime/publication notes are preserved in the archive and summarized below.
- Integration checks used isolated RTX 5090 workspace `validation/pr-merge-20261008`; existing radio services were not changed. September PIDs and inspection windows are historical.
- Separate UE checkout: `${HOME}/Documents/GitHub/srsran-sa-recovery`, last recorded branch `fix/sa-ra-recovery-5090`, tested source `daa167ae3443b046ce560df646c7dc5f17e5c1dd`, report `cb58e6085`. Its publication status has not been rechecked.

## Current Capabilities and Limits

- Rank-1 2×1/4×1 DL MISO and 1×2/1×4 UL SIMO work through the CUDA broker with a one-port srsUE. Synthetic captures prove multi-branch computation; the tested live DL radiates only gNB port 0, so live DL evidence is a scalar check.
- Identical and gain/phase-only matrix updates preserve CPU/CUDA history. Delay/layout changes reset history and warmup. Control delays are limited to 1023 samples with a 1031-sample device ring; dynamic CUDA matrices require the device-channel route.
- Sionna RT integration and one multi-gNB dashboard are merged. Delivery freshness uses two seconds; scheduler freshness remains five seconds. Connected, stale, empty and disconnected feeds are distinct.
- **Continuous moving two-UE traffic and strict zero-miss real-time qualification remain failed.** Stationary connectivity and bounded fresh-attachment recovery pass only in the documented configurations.
- Geometry changes are not seamless; full NR RLM, seamless handover and same-PRB MU-MIMO remain unqualified. OAI rank-2 gates and contributor evidence are now merged; srsUE remains rank-1, and live OAI trials were not repeated during integration.
- CMX500/X310 bridge tools and contributor attachment evidence are merged, but uplink timing remains late and IP traffic untested. Optional `RU-lite` remains unimplemented; corrected static two-cell connectivity passes while strict real-time qualification remains failed.
- Attached fractional-TDL hardware claims still require a common guard or a validated streaming filter: the centered eight-tap filter can zero-fill up to three future samples at block boundaries. Burst operation and X310-absolute updates need finite-tail, off-air advance and update interfaces.

## Validation Record

The latest row covers combined-tree checks on RTX 5090; other rows are historical. Synthetic broker checks do not establish live-radio or hardware qualification.

| Scope / revision | Recorded result | Limit |
|---|---|---|
| Project review, 2026-10-16 | CPU/CUDA 13/13 each; GPU 9/9; 221 Python tests; frontend, six browser views, 99 shell scripts and strict documentation checks passed | Three Python skips; no new live-radio, hardware or Docker qualification; [report](docs/reports/validation/project-review-20261016.md) |
| Full validation, 2026-10-09 | CPU/CUDA 12/12 each; GPU 9/9; 222 Python tests; frontend, Docker, packaging, documentation and five live connectivity configurations passed | Live runs recorded 2–11 starvations; OAI/CMX/GB10/Orin untested; [report](docs/reports/validation/full-validation-20261009.md) |
| Text cleanup, 2026-10-09 | Strict build; 114 fragment routes, 71 evidence files, 22 diagrams, 18 browser views; 23 tests and 19 subtests passed | Exactly 26 evidence path fields normalized; measurements preserved; existing commits unchanged |
| Whole-project cleanup, 2026-10-09 | Strict documentation and browser checks; 71 evidence hashes, 8 pixel-identical diagram views, 98 shell scripts, transfer/Docker checks, 4 tests and 19 subtests passed | Organization and upload filtering only; runtime and live-radio tests unnecessary and not repeated |
| Documentation migration, 2026-10-09 | Strict build, 114 fragment routes, 71 evidence hashes, desktop/mobile review; CPU/CUDA CTest 12/12 each, Python 211 passed/3 skipped, frontend and synthetic walkthroughs passed | Documentation and exercised synthetic commands only; live qualification unchanged |
| Structure migration, 2026-10-09 | CPU/CUDA CTest 12/12 each; GPU 9/9; Python 209 + 2 focused passed, 3 skipped; frontend, package, Docker and path checks; live rank-1 250/250 pings and matrix capture passed | Two live receive starvations; prebuilt radio images; native OAI/CMX/platform trials not repeated |
| PRs #2–#6 integration, 2026-10-08 | CPU/CUDA CTest 12/12 each; GPU sequence 9/9; Python 208 passed + one new focused test, 3 skipped; both frontend checks; 98 shell scripts | No new live-radio, CMX, GB10 or Orin qualification |
| Channel fixes `16af288` | CTest 12/12, Python 75/75, nine GPU stages, Node disconnect and shell checks passed; earlier extended CUDA sanitizer runs clean | Does not establish strict real-time operation |
| Main merge `e59e095` | Python 90/90, both Node frontend regressions and shell syntax passed | Launcher/dashboard checks; no new radio qualification |
| Cleanup `3bae52c` | Python 90/90, rank-1 renderer self-test, shell syntax and 61 documentation links/anchors passed | Executable behavior unchanged |
| Stationary two-gNB/four-port + two-UE/one-port Sionna/CUDA | Both UEs passed 250/250 pings on PCI 1 / PCI 2 | 56 starvations fail strict runtime gate |
| Moving SUTD, UE `daa167ae3`, 600.049 s | UE0: 58/58 perfect traffic batches; UE1: 19/58, with 36 zero-reply batches; final pings 10/10 and 0/10 | Continuous moving connectivity failed |
| UE `daa167ae3` | 18/18 focused tests; controlled UE0 loss recovered with new PDU and 10/10 replies in 3.974 s; three UE1 PCI 2 returning sessions passed | One PCI 1 return failed 1/10; no uninterrupted-connectivity claim |

Reports: [integration guide](docs/history/milestones/sionna-integration-20260914.md), [merge validation](docs/reports/validation/main-merge-validation.md), [channel fix validation](docs/reports/validation/sionna-merge-fixes-validation.md), [metrics recovery](docs/reports/validation/metrics-recovery-validation.md), and [rank-1 results](docs/history/translations/ko/rank1-feasibility-report.md).

## Completed Changes

### Pull-request integration — 2026-11-03

- Merged [PR #9](https://github.com/zhouyou-gu/ocudu-gpu-channel/pull/9) through a normal merge after its documentation CI passed; the dated GB10 broker report, datasets and measurement tools preserve contributor history.
- Replaced PR #10's unresolved directory links with its data-and-reproduction section link; the failed CI identified exactly those two warnings. Strict Sphinx and publication checks pass on RTX with both reports: 120 pages, 18,606 links, zero errors.
- Merged updated main into PR #10's integration branch and resolved the shared performance-index conflict by retaining both report entries. Contributor history remains intact; evidence: `validation/pr-10-merge-20261103/` on RTX.
- Merged [PR #10](https://github.com/zhouyou-gu/ocudu-gpu-channel/pull/10) after its corrected documentation CI passed, then fast-forwarded local main and verified both original contributor tips are ancestors. No open PRs remain.

### Project review — 2026-10-16

- Fixed the scheduler dashboard's single-cell crash by rendering the configured cells throughout setup, metrics, charts and scene labels. The regression fails on the original dashboard and passes for both execution modes.

- Restored report navigation and downloadable evidence logs; replaced the obsolete publication count with inventory and file-hash checks. All 287 hashes in the new artifact manifests match; CPU/CUDA parity wording now distinguishes numerical tolerance from bit-exact output.

- Validated CPU and CUDA CTest, 13/13 each, nine GPU stages, 221 Python tests and 99 shell scripts on RTX. Three environment-dependent Python skips remain; live-radio timing qualification was not repeated.

- Fixed tablet-width overflow in scheduler source labels. Chromium checks pass for single- and two-cell dashboards at 1440, 700 and 390 pixels; strict documentation and publication checks pass with the report logs included.

### Scheduler brief-burst comparison — 2026-10-14

- Four matched sequential runs delivered every packet without full RLC queues; QoS lowered control/video delay by 24%, increasing download delay. [Evidence](docs/reports/experiments/scheduler-bursty-load-20261014/README.md) retains timing misses and worst delays; the dedicated container was stopped.

### Scheduler contention on RTX — 2026-10-14

- Raised video bursts to 40 Mb/s in four sequential runs: QoS lost 68%/44% fewer control/video packets, with about 12% lower delay. [Evidence](docs/reports/experiments/scheduler-priority-load-20261014/README.md) records matched resources, download tradeoff and remaining starvations; experiment container stopped.

### Scheduler benchmark on RTX — 2026-10-13

- Repeated 70 Mb/s bulk-load tests sequentially: QoS control/video delay averaged 9%/12% lower; bulk delivery remained about 71% for both. Earlier settings and values matched; [evidence](docs/reports/experiments/scheduler-high-load-20261013/README.md) records OCUDU queue drops and remaining timing misses.

- Audited sequential RR/QoS runtimes: no channel overflow/gaps, but 15/9 starvations and OCUDU TX pauses up to 9.7 ms; QoS lost one control packet. Restored sources passed 13/13 RTX tests; [evidence](docs/reports/experiments/runtime-load-audit-20261013/README.md) records counter limitations.

- Traced two 120-second single-cell runs: pacing adjustments explain 123–132 ms of lag, predominantly following IQ input/output waits. Rare processing tails remain; [evidence](docs/reports/experiments/scheduler-pacing-trace-20261013/README.md) distinguishes measured stages from unresolved initiating causes.

- Fixed the broker summary's constant 5.005 ms maximum using existing exact timing data. The regression rejects the original implementation; RTX CTest passes 13/13, the source mirror is restored, and the dedicated container is stopped.

- Extracted the optional srsUE receive-sleep switch and verified corrected runs at 998.8/999.1 slots/s, with 8.9/8.7 ms RR/QoS delay and complete packet delivery. Timing waits remain; tests and [evidence](docs/reports/experiments/scheduler-broker-paced-20261013/README.md) are retained.

- Added opt-in single-cell execution, traffic filtering and analysis. RTX tests ran 28 cases with one skip; RR/QoS each completed 120 valid seconds, with delays 10.8/13.5 ms and IQ progress below nominal ([baseline](docs/reports/experiments/scheduler-single-cell-20261013/README.md)).

- Audited saved RTX IQ counters: 20.76–21.04 MS/s versus 23.04 nominal confirms slowdown. Concurrent cells shared CPU sets; real-time priority requests failed; contention remains an unproven cause ([audit](docs/reports/experiments/scheduler-20261013/radio-speed-audit.md)).

- Fixed the scheduler renderer's missing PRACH-root input for both independent cells. RTX CPU/CUDA tests passed 13/13; scheduler tests passed with one skip, and both live policy assignments completed with all four UEs attached.

- Measured two 120-second swapped scheduler runs: mean per-second bulk-DL p99 was 34.286 ms for RR and 27.865 ms for QoS. Radio speed was 90–91% of real time; receive starvations remain ([evidence](docs/reports/experiments/scheduler-20261013/README.md)).

### CPU sample workers — 2026-10-13

- Added opt-in `runtime.cpu_threads: 24`; defaults remain serial. RTX tests pass 13/13 with bit-exact prior-revision fingerprints and broker loopback; matched TDL-A p99 improves 56.85→9.79 ms, versus CUDA 0.231 ms ([evidence](docs/reports/performance/2026-10-13-cpu-24/README.md)).

### Vocabulary and slide review — 2026-10-13

- Replaced page 4's unexplained producer caption and reconciled page 7's TX/RX labels, channel-response sentence and equation definitions. The 25-page PDF is synchronized; package checks passed and both changed pages were visually inspected.

- Created `AGENT_VOCABULARY.md` with 82 entries, seven unmerged records and one harness preference. RTX checks passed for the exact template header, entry format, preference scope, whitespace and progress brevity; mission and control files remain unchanged.

- Reviewed vocabulary against the broker, configuration and IQ code; clarified topology, sample timing, taps, IQ power, memory and superposition. Removed two generic definitions and narrowed unresolved records to six; template and dependency-order checks passed on RTX.

### Project logo and docs preview — 2026-10-09

- Published the radio icon as `docs/assets/branding/logo.svg` for the README title, docs sidebar and favicon (`09a1b9f`); retired the old favicon and routed `/favicon.svg` to the logo. Strict Sphinx build, site check and Pages deploy passed.
- Added GitHub repository links to the docs sidebar and footer, and `scripts/docs/serve.sh` to build and serve the docs locally with Python 3.12 (`0faae43`). Fresh and repeat runs, strict build and Pages deploy passed.

### Contributor credits — 2026-10-09

- Added Quanlong Zhao to the README contributor summary and directory as a Research Assistant at SUTD, contributing hardware-in-the-loop testing. Markdown rendering and whitespace checks passed on RTX.

### Publication — 2026-10-09

- Published cleanup, launcher/dashboard fixes and validation records through `370e26e` to `origin/main`, preserving all twelve migration commits. RTX publication checks passed; no history rewrite or manual deployment.

### Full validation and launcher corrections — 2026-10-09

- Corrected static two-cell gNB dimensions and endpoint publication to match its scalar topology; preserved Sionna configuration bytes. The failing regression now passes, and both live UEs established sessions and passed traffic with zero ZMQ errors.

- Corrected the synthetic two-cell launcher's topology and complete peer list. All five Sionna scene modes applied updates without rejection; four multi-node browser checks passed, with one SUTD timing miss retained.

- Completed RTX CPU/CUDA, nine-stage GPU, 222 Python, frontend, Docker, package, publication and five live connectivity checks. All thirteen existing containers survived; live timing misses remain documented in the [validation report](docs/reports/validation/full-validation-20261009.md).

- Clarified the dashboard's illustrative architecture, software-UE labels and qualification limits. Readiness no longer implies live-radio, matrix or strict timing verification.

- Fixed mobile panel overflow and clipped tap columns with responsive grids and scrollable tables. Five viewport checks, installed assets, fifty dashboard tests and both frontend checks passed on RTX.

### Text cleanup — 2026-10-09

- Removed personal paths and shared-host details; replaced interaction narration with technical descriptions. Dated notes identify historical prose edits; existing commits and private originals remain intact.

- Published three evidence derivatives with 26 normalized path fields and original/public hash provenance; all other values are unchanged. Added publication checks and regression coverage; RTX build, browser, integrity and focused tests passed (`validation/text-redaction-20261009/`).

### Text disclosure review — 2026-10-09

- Reviewed 638 source texts, 382 publication texts and 2,632 historical blobs on RTX. No confirmed credential leak; personal-path and agent-narration findings remain unresolved (`validation/text-review-20261009/`); source and evidence were not redacted.

### Whole-project cleanup — 2026-10-09

- Removed twelve remaining source stubs and repaired current documentation paths; generated website routes remain available. RTX strict build, 114 fragment routes, browser review and 71 evidence hashes passed.

- Shared upload exclusions across eight scripts and corrected recursive Docker cache exclusions; updated the attachment driver checksum after verifying its single argument change. Transfer boundaries, Docker inputs and native fixture checks passed on RTX.

- Removed generated local clutter, empty migration directories and 51 unused CSS selectors. Eight diagram views remain pixel-identical; research, writing, settings and historical evidence are preserved (`validation/cleanup-20261009/`).

### Root milestone cleanup — 2026-10-09

- Removed six root milestone stubs and updated current links and compatibility requirements. RTX strict build and publication checks passed; history and evidence are unchanged (`validation/docs-20261009/root-stub-cleanup/`).

### Documentation migration — 2026-10-09

- Completed documentation validation on RTX: strict build, legacy routes, evidence hashes, responsive preview, CPU/CUDA tests and synthetic walkthroughs passed. [Report](docs/reports/validation/documentation-migration-20261009.md) records skips and unchanged live limits; commits remain local.

- Organized reports, evidence bundles, historical designs, milestone ledgers and Korean originals; retained published source pointers and updated figure destinations. Original evidence bytes are preserved; strict build, compatibility and operational walkthrough checks are next.

- Split the technical reference into focused concepts, interfaces and guides; added the CUDA newcomer path, qualification table and concise README gateway. Preserved diagrams and added source-backed configuration, control and telemetry definitions; historical relocation and validation remain.

- Added a pinned Sphinx/MyST/Furo build, separate PR validation and Pages deployment, repository-link handling, legacy routing and evidence checks. Content migration is next; full site validation follows the completed move.

- Inventoried documentation, technical-reference sections, 82 legacy IDs and 22 diagrams; recorded evidence hashes, destinations and page ownership before migration. Publishing and content conversion are next.

### Structure migration — 2026-10-09

- Completed remote CPU/CUDA, GPU, Python/frontend, package, Docker, patch and path validation; deterministic IQ output matches the baseline byte for byte. Details: [migration report](docs/reports/validation/structure-migration-20261009.md).
- Live rank-1 attachment, PDU, 250/250 pings and independent matrix capture passed using pinned prebuilt images; two receive starvations still fail strict real-time qualification. Recorded provisioning limits and restored the original container inventory.

- Added application/integration indexes and documentation navigation for guides, reports, plans and milestone history, preserving published document locations. Recorded the approved responsibility boundaries in the harness and added patch-lock relocation regressions.

- Grouped tests by component and moved the mutation-cost benchmark into `benchmarks/`; updated CMake and test path resolution. Full remote validation is next.

- Consolidated external patches and revision locks under `integrations/`; moved USRP source and CMX workflows to their assigned components. Patch contents and source pins are unchanged; pre-migration OAI module manifests require rebuilding because they include patch paths.

- Moved the bridge and dashboard into `apps/`, retained launch orchestration in `scripts/local/`, and added installable packages with independent dependency extras. Source entrypoints remain available; installed commands resolve input paths from the launch directory.

- Renamed `examples/` to `use_cases/` and updated operational references, Docker paths and native fixture pins. The pinned driver differs only by the intended directory substitution; remote checks remain pending.

- Recorded the approved destination map and command interfaces in `docs/development/project-structure.md`; preserved the earlier planning note. Migration and isolated RTX validation are in progress.

### Structure planning — 2026-10-09

- Reviewed source, Python components, workflows, configurations and documentation; proposed separating runtime packages, external integrations and operational scripts. Prepared a staged migration plan in chat; implementation has not started.

### Pull-request integration — 2026-10-08

- Published the integrated tree through `8ecbd5d`; GitHub confirms PRs #2–#6 merged, with contributor history preserved and no open PRs remaining.
- Fixed rank-1 fixture checks broken by configuration relocation, retaining all four content pins; mutation checks reject changed files. Added coexistence coverage for CMX pacing/queues and CUDA/scaling options; CPU/CUDA checks passed.
- Combined-tree validation passed CPU/CUDA CTest, nine GPU stages, Python/frontend and shell checks. Evidence: `~/ocudu-gpu-channel-workspace/validation/pr-merge-20261008/`; optional rendering/integration tests were skipped, and live hardware claims remain bounded to contributor reports.
- Merged draft PR #5 locally: CMX500/X310 bridge, srsUE patches, optional broker pacing and per-device TX queues. Preserved CUDA/carrier settings and moved CMX configs into the common layout; hardware timing and IP-traffic limits remain documented.
- Merged PR #6 locally: scheduler benchmark, robot demo and example reorganization. Combined conflicting branches without dropping X-track functionality; updated added configurations and references to the new layout while preserving the concise agent files.
- Merged PR #4 locally: physical carrier checks, transmit scaling, CFO correction, TDD interference gates and threaded lower-PHY patches. Retained concise agent files and the contributor’s upstream-patch rule; detailed evidence remains in milestone documents.
- Merged PR #3 locally: integrated-GPU zero-copy, CUDA initialization stream ordering, OAI MIMO gates and D10 vendor patches. Contributor live results remain platform- and revision-specific; combined-tree validation passed as recorded above.
- Merged PR #2 locally with contributor history intact: selectable CUDA gNB profiles for RTX 5090, GB10 and Orin. Preserved the user-reset mission; contributor evidence is in `CUDA_MILESTONES.md`, `SPARK_MILESTONES.md` and `JETSON_MILESTONES.md`.

### Rank-1 implementation and review — August 2026

- Added two-/four-port topologies, synthetic branch-isolation/coherent-combining gates, live OCUDU/Open5GS/srsUE gates and independent wire `y = Hx` scoring. Preserved the 1×1 regression.
- Adapted gNB fixtures for srsUE: dedicated DCI 0_1/1_1, CSI-RS disabled with zero CSI resources, MAC pcap disabled and `pusch.max_ue_mcs: 9`. The MCS cap removed the standing UL error rate; 4T4R then attached 3/3 with zero PUSCH KO.
- Proved full synthetic DL/UL vectors and live multi-branch UL. Explicitly declared silent live DL branches. Oracle weight experiments matched predicted power; they do not demonstrate closed-loop PMI.
- Ported native rank-1 gates to containerized remote scripts. Fixed subnet selection, SSH argument transport and missing numpy/PyYAML provisioning.
- Replaced once-per-second latency snapshots with whole-run histograms, corrected published envelopes and remapped private evidence hashes to published commits. August review passed GPU 9/9, CPU/CUDA CTest 8/8 each, sanitizer checks and live 1×1/2×1/4×1 gates; strict starvation limits remain separate.
- Generalized multi-UE smokes and added the four-UE topology, per-UE IMEIs, free-subnet selection and the user-fork PRACH preamble override. Staggered four-UE attach passed 4/4; simultaneous launch passed only 3/4. Reverted ineffective broker experiments.
- Completed the MIMO/rank-1 assessment, Sionna export plan and CMX500/X310 feasibility/pipeline study. Hardware P0/P1 inventory and direct stock-UHD attachment remain prerequisites to adapter work.

### Sionna integration — 2026-09-13–14

- Merged contributor history normally, then fixed unnecessary matrix-history resets. Tests reproduced lost delayed echoes before the fix and checked all five antenna shapes.
- Fixed four reviewed defects: CUDA delay capacity, silent host-staging matrix fallback, the supplied SUTD launcher's antenna topology, and stale delivery/HTTP readiness. Static mixed processing remains supported; unsupported dynamic routes reject updates before state mutation.
- Added deterministic NVML fixtures and corrected remote sync exclusions. Long-delay regression failed against reviewed `88cbe6d` and passed against fixed source.
- Completed uncontended 600-second reviewed/fixed timing windows. Both retained deadline misses; moving traffic failed despite initial PDU sessions. Stationary traffic passed with nonzero starvation. Full configurations and measurements are in the channel fix report.

### Dashboard and UE recovery — 2026-09-14

- Unified independent gNB feeds in `5c6cafd`: repeat `--gnb-metrics-source ID=URL`; `ran_gnbs` exposes per-gNB state and legacy `ran` remains the first feed.
- Fixed quiet-socket reconnects in `d6637f7` using Ping after five seconds idle and matching Pong within ten seconds. Partial frames survive polling; cached UE rows remain historical until a new-session scheduler report.
- Metrics validation covered 603 seconds and deliberate independent feed interruptions: 83/83 heartbeats per gNB, no unplanned reconnects, no browser reload/errors. Scheduler staleness remained visible; radio recovery was still failing at that checkpoint.
- Diagnosed missing SA recovery, by-value NAS session reset, unsafe network Release handling and an SFN retry budget that accumulated across selections. Commits `4c8189b6c`, `5f0ac625b` and `c70ae415b` added deferred recovery, bounded retries/watchdogs, rejection/timer handling and fresh selection budgets.
- `c70ae415b` passed eight controlled outages, including 120-second blackouts, with fresh PDU, ten replies within 60 seconds, unchanged UE processes/IPs and other-UE traffic. Earlier failed runs remain archived. The full NR-RRC PLMN/setup fixture failure occurred on both baseline and fixed source.
- Follow-ups `94cd6e71d`, `4c9b0c55f` and `daa167ae3` added decoded-slot realignment, deferred PRACH, N310/N311/T310 handling and routing through the actual combined LTE/NR stack. The intermediate `4c9` image was never deployed.
- Final moving validation demonstrated bounded recovery and cross-gNB fresh attachment, but intermittent UE1 traffic persisted. All ten CUDA links advanced; both feeds stayed connected with 23 stale scheduler samples each. Strict real-time qualification was not rerun or relaxed.
- UE reports: `docs/sa-recovery-validation.md` / `sa-recovery-results.json` at `a2d92028d`, superseded by `docs/sa-sync-recovery-followup.md` / `sa-sync-recovery-results.json` at `cb58e6085` in the separate UE checkout. Full hashes and failed/passing evidence remain there and in the archive.

### Main consolidation and publication — 2026-09-14

- Merged integration `0e0a6c3` and retained launcher tip `5f55bb7` into this folder. Resolved the multi-gNB launcher conflict, restored legacy Sionna arguments and fixed SSH/empty-core-port transport; four regressions cover those paths.
- Preserved pending edits in `.git/merge-backups/main-20260914T075710Z` and stash `5021c39660e39aab3b5dd9784a8c2f4e399300aa`. Reconciled older UI/hold behavior with integrated implementations.
- Added the current integration guide and normalized README/reference credits: Zhouyou Gu and Jihong Park (SUTD), Minwoo Eun and Hyunsoo Lee (Yonsei University). README owns public contributor metadata; Git authorship remains intact.
- Shortened README while preserving all 12 comparison rows, eight command blocks and credits. Nine-document checks passed 147 links/anchors and desktop/mobile previews; evidence is in `validation/readme-cleanup-20260914`.
- Archived/hash-verified the obsolete worktree and six generated folders before removal. Backups: `.git/cleanup-backups/20260914T080717Z`; pointer `latest.txt`. Removed four local and three origin branches only after ancestor checks; tips are in `.git/branch-cleanup-20260914T081423Z.json`.
- Removed obsolete development notes and the unused QuickJS archive; consolidated native operations in `scripts/native/README.md`, repaired nine archive links and retained historical qualifications. Pre-cleanup source is `5ffd73e`; README and executable behavior were unchanged.
- Published integration, credits and cleanup with user-authorized normal pushes, ending at `3bae52c`. No tag or release was created.

### Last recorded user demo — 2026-09-14

- Ran the original moving SUTD two-gNB/four-port, two-UE/one-port scenario using CUDA and UE `daa167ae3`, with Sionna connected directly to broker port 5559.
- Both UEs established PDU sessions at 10.45.1.2/.3 and initially passed 10/10 pings. Both were served by gNB0; gNB1 reported a fresh empty list. These startup checks did not supersede moving/deadline failures.
- One dashboard was exposed at `http://127.0.0.1:19080` from 19:27 until a scheduled cleanup around 20:28 SGT. Thirteen unrelated containers were preserved. Current liveness is unknown.
- Evidence: `~/ocudu-gpu-channel-workspace/validation/user-demo-20260914T112712Z`; pointer `validation/current-user-demo.txt`. The pending demo and cleanup-publication notes are preserved by this condensation.

### Agent-file condensation — 2026-10-08

- Shortened harness rules, removed repetitive examples and grouped related guidance. Retained operating constraints and recorded the preference for concise rules and outcomes.
- Consolidated progress into current state, qualification limits, revision-specific validation and completed outcomes. Archived the entire prior ledger, including uncommitted notes, with only a historical banner and relative-link adjustments.
- Documentation checks on RTX 5090 recovered the original progress exactly from the archive, resolved all 14 local Markdown links and found no edited-file whitespace or conflict-marker errors. No mission/control, source or runtime changes.
- Added explicit progress-entry limits: one bullet, two sentences and 40 words per meaningful change, with a required final brevity check. The progress header links to the authoritative harness rules.
- User amended the mission on 2026-10-08, resetting `AGENT_GOAL.md` following the supplied reference.
- User amended `AGENT_GOAL.md` by removing the reset note on 2026-10-08.
- Published agent-file updates as `1b46173` to `origin/main` on 2026-10-08 at the user's request. Fetch found no remote-only commits; whitespace checks passed, and source/runtime files were unchanged.
- Removed the introductory paragraph at the user's request; the archive remains linked under Evidence and Archives. Whitespace checks passed, and the user authorized commit and push.

## Evidence and Archives

Remote evidence roots below are under `~/ocudu-gpu-channel-workspace/`:

- `validation/full-20261009/`: source snapshots, full test matrix, before/after launcher regressions, live radio reports, scene/dashboard checks, packaging and publication artifacts, namespace/fixture provenance and container-preservation checks.

- `validation/text-redaction-20261009/`: sanitized artifact, scan results, browser review, regression logs and original/derivative comparisons; private originals are also retained locally in `.git/text-redaction-backups/20261009/`.

- `validation/text-review-20261009/`: value-redacted text audit, known-configuration matching, reachable-history scan and review findings.

- `validation/cleanup-20261009/`: cleanup mirror, source/evidence integrity checks, Docker-context and transfer checks, fixture results and before/after diagram/browser screenshots.

- `validation/docs-20261009/`: documentation artifact, compatibility checks, browser screenshots, CPU/CUDA and Python/frontend results, tutorial and Sionna/dashboard walkthroughs.

- `validation/sionna-history-20260913/fix-validation/`: integration, channel and dashboard evidence.
- `validation/ue-sa-recovery-20260914/`: UE builds, controlled outages, moving runs and final `sync-loss-live/` results.
- `validation/main-merge-20260914/`, `validation/docs-contributors-20260914/`, `validation/readme-cleanup-20260914/` and `validation/release-cleanup-20260914/`: merge and documentation checks.

Historical ledgers:

- [Full record before this condensation](archive/progress/AGENT_PROGRESS-before-condense-2026-10-08.md).
- [Upstream through 2026-08-16](archive/progress/AGENT_PROGRESS-2026-08-16.md).
- [Rank-1 branch through 2026-08-17](archive/progress/AGENT_PROGRESS-rank1-miso-simo-2026-08-17.md).
- [Sionna contributor at 066a702](archive/progress/AGENT_PROGRESS-ehs0-066a702.md).

## Next Resume Point

No pull-request integration work remains queued. Strict zero-starvation qualification remains failed; the existing scheduler reports retain the unresolved IQ handoff pauses and separately confirmed full-RLC-queue packet drops.
