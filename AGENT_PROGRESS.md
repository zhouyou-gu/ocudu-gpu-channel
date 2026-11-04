# Progress

> **Entry rules:** One bullet per meaningful change; at most two sentences and 40 words. Record outcomes, decisive checks and blockers; link details and omit repeated context. Apply the [harness rules](AGENT_HARNESS.md#progress-entries) before finishing.

## Repository State

- Reviewing and publishing the demo and dashboard fixes. Review found and fixed chart canvases being cleared by status-card rebuilds; the new functional regression and existing frontend checks pass on RTX.

- The Spark viewing run now uses the dashboard fixes for #21: lightweight status polls, compressed responses and route-focused framing. Browser screenshots confirm live feeds and visible nodes/rays; changes and the refreshed embedded-patch handoff remain uncommitted.

- The requested Spark SUTD demo is live at `http://localhost:18080` until approximately 14:30 Singapore time on 4 November 2026. Sionna and all ten telemetry edges are fresh; the demo and forwarding helpers have finite lifetimes.

- The SUTD synthetic crosstalk demo is implemented and Spark-verified on `c775de8` plus local changes. The self-contained handoff prompt embeds the tested patch; demo code remains uncommitted, with a requested viewing run active.

- The scheduler broker-environment truncation defect (#20) is fixed and published in `fd0e61a` on `origin/main`. All eight mock-child cases and the RTX helper suite pass; GitHub #20 is closed as completed with the fix linked.

- Ten open GitHub issues (#11–#19 and #21) and resolved #20 have numbered technical records in `docs/issues/`. Issue #21 tracks the current unpublished dashboard timeout fix; earlier issue records are published.

- Live experiments are stopped and the reviewed changes are published. RTX passes 13/13 core checks; Spark passes 12/13 with an existing CPU-reference failure, and its periodic slot delays remain unresolved.

- Broker clock recovery and notified ring handoff are published in `fd0e61a`. Corrected transport placement reached 1000.320 slots/s; strict timing still fails, with traced remaining delays in display kernel work and UE SYNC dispatch.

- Local presentation artifacts remain in ignored `slides/`; the directory is also excluded from uploads and Docker builds. Vocabulary contains 96 definitions, including recovery rate and look-ahead/catch-up margins, and six unresolved wording or protected-name records.

- The [runtime limitation record](docs/reports/validation/live-runtime-limitations-20261104.md) consolidates RTX timing and Spark CPU-radio capacity findings. Strict real-time qualification remains failed; no experimental scheduling or pacing settings were promoted to production.

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
- **Continuous moving two-gNB/two-UE traffic and strict zero-miss real-time qualification remain failed.** The 2026-11-03 single-cell/two-UE moving run delivered every packet over 600 seconds while accumulating 1.517 seconds of radio-time lag.
- Geometry changes are not seamless; full NR RLM, seamless handover and same-PRB MU-MIMO remain unqualified. OAI rank-2 gates and contributor evidence are now merged; srsUE remains rank-1, and live OAI trials were not repeated during integration.
- CMX500/X310 bridge tools and contributor attachment evidence are merged, but uplink timing remains late and IP traffic untested. Optional `RU-lite` remains unimplemented; corrected static two-cell connectivity passes while strict real-time qualification remains failed.
- Attached fractional-TDL hardware claims still require a common guard or a validated streaming filter: the centered eight-tap filter can zero-fill up to three future samples at block boundaries. Burst operation and X310-absolute updates need finite-tail, off-air advance and update interfaces.

## Validation Record

The latest row covers combined-tree checks on RTX 5090; other rows are historical. Synthetic broker checks do not establish live-radio or hardware qualification.

| Scope / revision | Recorded result | Limit |
|---|---|---|
| Live margin comparison, 2026-11-03, isolated `c3135a9` prototype | 600-second default/fixed/dynamic lag: 21.982/10.010/17.967 s; 1 ms output peaks and zero detected overflow/gap/ZMQ errors; 13/13 tests in six builds/runs | Shared-host activity varied; fixed bulk p99 175.8 ms versus default 30.0 ms; 20-second four-port traffic passed, with three silent-UL capture failures; [report](docs/reports/validation/live-runtime-limitations-20261104.md) |
| Live slot timing, 2026-11-03, `c3135a9` | 600-second baseline: 997.472 slots/s, all 3,229,424 packets delivered; 300-second boundary diagnostic; both builds pass 13/13 RTX tests | Zero overflow/gaps/ZMQ errors; 72 baseline lifetime starvations fail strict real-time qualification; [report](docs/reports/validation/live-runtime-limitations-20261104.md) |
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

### Demo publication review — 2026-11-04

- Found a chart regression during review: status-card rebuilds replaced cached chart data with empty status history. Routed chart redraws through the dedicated response and verified the functional regression plus disconnect and RAN checks on RTX.

### Live dashboard connection and camera repair — 2026-11-04

- Reproduced false UI disconnections: two-second history produced a 12.8 MB status response taking 3.88 seconds against a two-second browser timeout. Created GitHub #21 and a local issue record after checking for duplicates.
- Removed raw chart history from status polling and enabled negotiated HTTP compression. Live status measured 27 KB/24 ms and chart history 92 KB/162 ms; browser reload showed all ten telemetry and CUDA readiness indicators active.
- Fitted the opening camera to node positions and mobility routes, preserving a Campus view. Visually inspected moving nodes and rays; RTX passed 44 Python dashboard tests plus the frontend disconnect/status-payload regression.
- The renewed viewing run uses launcher 99844, relay 75161 and SSH tunnel 41352, with finite lifetimes. Logs remain ignored under `/workspace/codex-sutd-demo-20261104/view-fix/`; the browser screenshot is in the local ignored handoff folder.
- RTX also passed the frontend RAN regression and 28 scheduler/bridge helper tests with two skips. Strict documentation checks passed: 135 pages, 21,461 links and zero errors; the embedded handoff patch now includes the UI fixes.
- Applied the refreshed handoff patch to clean Spark source and verified all six implementation files match the tested tree. Removed 880 MB of completed viewing logs after saving a compact summary; current viewing logs remain ignored.

### Fresh Spark handoff assumption — 2026-11-04

- Revised the guide and embedded-patch prompt for a factory-initialized Spark host without added software. Installation now precedes cloning and building; Docker is optional, and CUDA toolkit, OptiX, Python and host SSH checks are explicit.
- Recorded the fresh-machine handoff rule in the harness; strict RTX documentation checks passed with zero errors. Existing-container runtime tests remain valid; factory-reset provisioning has not been executed or claimed as tested.

### Spark demo viewing window — 2026-11-04

- Started the ten-minute synthetic SUTD demo and a finite relay/tunnel to local port 18080. Verified live Sionna updates, fresh telemetry for all ten edges and finite crosstalk power in both directions at 1.9 GHz; requested the browser panel.
- Viewing sessions are launcher 71020, container relay 76546 and SSH tunnel 72530. Runtime logs remain under `/workspace/codex-sutd-demo-20261104/show/`; existing services are preserved, and real-radio qualification is unchanged.

### Portable SUTD crosstalk demo — 2026-11-04

- Added a ten-edge shared-carrier configuration generator, a synthetic launcher case and a completed-run verifier. Preserved SUTD routes and antenna dimensions; all edges trace at 1.9 GHz, with configurable service ports and occupied-port checks.
- Fresh GB10 CUDA build and 45-/20-second synthetic runs passed. The final run accepted 196 batches, both UEs moved and both crosstalk paths had finite power; one starvation remained, with zero overflow, sequence gaps or ZMQ errors.
- Confirmed dashboard HTTP/status data, clean port release and preserved existing services. Browser rendering was not inspected; these checks establish synthetic observability, not scheduled TDD interference, radio attachment or strict real-time qualification.
- Added the operating guide and an ignored self-contained handoff prompt with the complete patch. Verified clean application on Spark and matching script hashes; removed expanded run logs, retaining a 7 MB compressed evidence bundle and compact summaries.
- RTX launcher syntax and strict documentation checks passed: 134 pages, 21,282 links and zero errors. The self-contained patch applies to the clean published base; no commit or push was performed.

### Cleaned publication and issue closure — 2026-11-04

- Committed and pushed 35 reviewed source, test and documentation files as `fd0e61a`; verified the remote `main` hash. Logs, raw traces, IQ captures and generated artifacts were excluded, with existing ignore rules retained.
- Closed GitHub #20 as completed after linking the published launcher fix and eight-case regression verification. Updated its local record and moved it to the closed-issue index; issues #11–#19 remain open.
- Removed stale publication notes from issue records and normalized Markdown whitespace. Strict RTX documentation checks passed after closure: 133 pages, 21,107 links and zero errors; live experiments remained stopped.

### Broker environment tokenization fix — 2026-11-04

- Replaced first-line parsing with the resolver's Python whitespace splitting and a quoted Bash array. Multiline, leading-newline and whitespace-only settings now reach the launch correctly; values remain literal.
- Expanded the actual-launch regression to eight child-environment cases. Before the fix, four mismatches and one launch failure reproduced; afterward all cases passed, with 28 helper tests passing, two skipped and Bash syntax valid on RTX.
- Updated GitHub #20 and its local record with fix verification and pending publication. The changes remain uncommitted; live experiments stayed stopped and raw test output remains ignored.
- Strict RTX documentation build and site checks passed with zero errors. Whitespace checks passed, and raw before/after test logs remain covered by Git ignore rules.

### Pending-change review — 2026-11-04

- Confirmed a P2 launcher defect on RTX using the actual launch block and a mock child: newline-separated broker settings lose later assignments, despite resolver acceptance and complete run metadata. A leading newline drops every assignment.
- Created GitHub issue #20 and its local record; runtime implementation was left unchanged. Reviewed pacing, notifications, configuration, tests and issue documentation without identifying another introduced defect; existing timing and ARM CPU-reference limits remain separate.
- The three mock-launch cases completed on RTX without starting radios; single-line settings arrived intact, while multiline and leading-newline settings were truncated. Strict documentation checks passed: 133 pages, 21,092 links and zero errors.

### GitHub issue tracking — 2026-11-04

- Added harness rules requiring GitHub tracking and concise numbered records in `docs/issues/`, with deduplication, evidence, closure checks and synchronized updates. Bulk logs, raw traces and private details remain excluded.
- Created GitHub issues #11–#19 for the nine documented open problems, after checking for existing issues. Each has a local record separating observed behavior, confirmed findings, unknowns and closure checks; no new runtime experiment was started.
- Linked the issue index from documentation navigation, developer guidance and project status, and linked current runtime findings to their issues. Local documents are uncommitted; GitHub issues are live.
- Verified all nine GitHub issue numbers, titles and open states. Strict RTX documentation checks passed: 132 pages, 20,919 links and zero errors; runtime code and qualification were unchanged.

### Spark timing test and cross-platform review — 2026-11-04

- Built the reviewed clock-recovery source for ARM/SM 121 and completed two 30-second Spark runs; both connectivity gates passed without overflow/IQ gaps/ZMQ errors. Excluded one setup attempt that missed the measurement lead.
- The reduced-audit run reached gNB 999.979 and UEs 1000.115/1000.135 slots/s, delivering 160,962/160,963 packets. Four roughly periodic 8.872–9.749 ms slot gaps and 14 whole-process starvations remain; the cause is unresolved.
- Restricted background applications to CPUs 0–4, with three placement snapshots and periodic enforcement. Retained audit events showed no competing application CPU time; 120 lost CPU-5 events prevent complete causal or interruption-free qualification.
- Reviewed notification/pacing/shutdown/launcher changes; RTX CTest passed 13/13 and Spark 12/13. Spark's CPU-reference IQ comparison also fails in the old build with identical CPU sources; helper tests passed 28 with two skips on each platform.
- Stopped live experiments at the user's request, restored settings and removed the helper, preserving four Spark containers. Pruned 2.30 GB, retained 0.762 MB of ignored traces and confirmed the earlier demo/tunnel stopped; no code was committed or pushed.
- Recorded Spark timing limits and the cross-platform review; six placement snapshots, three retained trace hashes and cleanup counts passed verification. Strict RTX documentation checks passed: 122 pages, 19,160 links, zero errors.

### Remaining delay mechanisms after transport placement — 2026-11-04

- Captured a 7.315 ms slot gap: bound display polling on CPU 7 delayed FIFO gNB I/O dispatch by 6.215 ms after the broker had sent IQ. The unbound workqueue mask and IRQ placement did not move this callback.
- Captured a 6.828 ms slot gap: UE SYNC waited 5.934 ms behind physical-AI Python on CPU 19, delaying uplink input to the broker. MAC took 10/18 µs and overlapping CUDA calls stayed at or below 0.548 ms.
- The 30-second diagnostic delivered all 160,963 packets with no detected overflow/IQ gaps/ZMQ errors or a hang; 26 whole-process starvations remained. These are captured examples, not attribution of every historical miss or strict timing qualification.
- Restored settings, removed probes and stopped owned containers, preserving 14 others. Total cleanup freed 4.22 GB and retained 2.23 MB of selected ignored traces; no runtime scheduling default or external stack source changed.
- Recorded both causal timelines, transport controls and remaining limits; defined OS versus MAC scheduling in the glossary. RTX checks verified source/evidence hashes, restoration, five ignore cases and strict documentation: 122 pages, 19,158 links, zero errors.

### Radio and UE transport placement — 2026-11-04

- Isolated gNB radio and gNB/UE ZMQ I/O on CPUs 6–9, excluded Python threads, and raised transport priorities for 30 seconds. FIFO radio polling worsened full-window progress to 974.785 slots/s, with 34.642 ms maximum consecutive slot gaps.
- All 160,963 packets arrived without detected overflow, sequence gaps, ZMQ errors or a hang; 83 whole-process starvations remained. Restored temporary settings; the second control used radio nice −10 with FIFO reserved for blocking I/O.
- The corrected control reached gNB 1000.320 and UEs 1000.283/1000.258 slots/s, with an 8.352 ms maximum slot gap. Nine gaps exceeded 5 ms, 55 whole-process starvations remained and two packets were missing; strict timing still fails.
- Restored all changed settings and verified 139 surviving Python threads; stopped both diagnostic containers, preserving 14 others. Pruned 3.11 GB and retained 1.34 MB of selected traces; a new requested causal trace has restarted the owned containers.

### Residual slot-delay tracing — 2026-11-04

- Completed the bounded 30-second trace; the traffic gate passed and consecutive slot gaps measured 4.150 and 4.951 ms. Removed owned probes, restored temporary settings and completed analysis and bulk cleanup.
- Captured a 3.773 ms runnable wait in the gNB radio thread behind Python work, and roughly 3 ms waits in UE transport threads. MAC computation took 11–27 µs; selected evidence stays outside Git, with compact causal results recorded.

### Bounded broker clock recovery — 2026-11-04

- Separated the nominal clock from release-rate limiting; missed time is retained while immediate credit stays one batch and recovery is capped at 1%. Added actual-publication accounting, fragment remainder carry, visible lag metrics and configuration parsing.
- RTX CTest passed 13/13; two mutations restoring nominal rebasing or omitting actual-publication accounting failed as intended. Synthetic-clock regressions cover bounded bursts, recovery, late wakes, fragmentation and 24-hour duration arithmetic.
- One 30-second run reached gNB 999.374 and UE0/UE1 999.534/999.503 slots/s; all 160,963 packets arrived with zero detected overflow/gaps/ZMQ errors or a hang. One-batch output peaks remained; 42 whole-process starvations prevent strict qualification.
- Independent gNB scheduler records show 52.394 ms recovered over 9.996606 seconds at 1005.241 slots/s, within the 1% allowance. Retained gauges include startup offsets and did not clear; no nominal clock reset was applied.
- Restored temporary scheduling/IRQ/workqueue settings, stopped both diagnostic containers and preserved 14 others. Pruned another 1.01 GB plus the unarmed observer buffer; detailed logs, curves and helpers stay ignored.
- Recorded compact recovery results and source hashes; updated timing/configuration guidance and the 89-entry vocabulary/glossary. Strict RTX documentation checks passed: 122 pages, 19,147 links and zero errors; the changed diagram retains original provenance.

### Radio timing source audit — 2026-11-04

- Confirmed that one-batch pacing credit and deadline rebasing make longer IQ-handoff delays persist as clock lag; an RTX probe of the actual header reproduced a 3 ms lasting shift after 4 ms lateness.
- Recorded sequential OCUDU ZMQ waits, broker sleep polling/CPU placement, fixed usable look-ahead and unused profile broker settings. Corrected timing/pipeline documentation and its nominal-schedule SVG; runtime code and experimental defaults remain unchanged.
- RTX source-probe/report checks and strict documentation validation passed: 122 pages and 19,143 links. The diagram inventory retains original provenance and records the corrected nominal-timing labels.

### Broker timing repair — 2026-11-04

- Replaced default broker ring sleep polling with targeted notifications and generation tickets; explicit diagnostic polling/spinning remains available. Four-port IQ comparison passed across all three modes.
- Scheduler broker launches now apply resolved profile settings and record them. RTX helper checks passed 28 tests with two skips; the new child-environment regression rejects the original launcher.
- Changed starvation accounting to check elapsed wait before accepting resumed input. Its transient-input regression rejects the original ordering; final CUDA-build CTest passed 13/13.
- Four completed 30-second controls delivered all 160,963 packets each without overflow, gaps, transport errors or a hang. Final rates were gNB 993.628 and UEs 993.661/993.647 slots/s; 22 whole-process starvations remain, with no established timing gain.
- Rejected the worse CPU 6 allocation; restored thread, IRQ and workqueue settings and stopped both diagnostic containers, preserving 14 others. Pruned 4.16 GB plus unarmed buffers; two setup-only attempts are excluded and all detailed evidence stays ignored.
- Compact source hashes/traffic totals, launcher syntax and strict RTX documentation checks passed: 122 pages, 19,145 links and zero errors. The working-tree repair remains unpublished and the one-batch clock policy unchanged.
- Added harness guidance for deadline accounting on resumed waits and preventing lost notifications when replacing polling.

### ZMQ I/O priority test — 2026-11-04

- Completed 30 seconds with ZMQ I/O FIFO 20 on CPU 7, PHY FIFO 10, and other test gNB threads on CPUs 1–6. Common interior rates were gNB 993.450, UE0 993.501 and UE1 993.503 slots/s.
- All 160,963 packets arrived without detected overflow, sequence gaps, ZMQ errors or a hang; 38 whole-process starvations and 6.872 ms maximum logged slot gaps remain. Retained I/O wake/run pairs peaked at 187 µs; earlier trace history was overwritten.
- Restored scheduling/IRQ/workqueue settings and stopped diagnostic containers, preserving 14 other containers. Pruned 1.02 GB; helpers, summaries and the 2.07 MB trace stay ignored, with only compact results added to the limitation record.

### Cleanup publication — 2026-11-04

- The authorized main publication contains compact limitation/status records and artifact-exclusion rules; detailed evidence remains outside Git. RTX documentation, five focused checks and 17 ignore cases passed; Git refs identify the delivery revision and remote state.

### Runtime cleanup and status review — 2026-11-04

- Archived and hash-verified five timing bundles and temporary helpers outside tracked source; removed 263 files from the working tree. Retained one compact limitation report and numeric summary, preserving separate Spark presentation/demo work.
- Removed the report-log Git exception and ignored new logs, JSONL streams, raw traces and IQ captures. Updated README/status to distinguish successful single-cell delivery from failed real-time/two-cell qualification.
- RTX checks passed archive integrity for 263 files, 17 ignore cases, five publication/upload checks and strict documentation: 122 pages, 19,136 links, zero errors. No radio qualification test was repeated.

### Spark demo preparation — 2026-11-04

- Live demo runs in Spark’s `ocudu-minwoo`, stamp `20261104T012811Z`; dashboard is http://127.0.0.1:18092/. Viewing ends automatically at 09:35:38 SGT; owned supervisor and tunnel read matching deadline markers.
- Both UEs established PDU sessions and passed initial traffic; pedestrian coverage later failed with zero traced rays. Car traffic continues; a 24-second sample measured approximately 998.1 nominal slots/s with timing misses.
- Corrected receive-sleep placement in generated ZMQ device arguments and stopped the failed attempt. The SUTD mesh renders in the live browser; existing containers and older dashboards are preserved.
- User chose live ray tracing and five minutes of viewing. First launch reached live Sionna updates and the dashboard, but both UEs rejected `rf.rx_sleep`; the receive-sleep setting belongs in ZMQ device arguments, and the failed attempt is being stopped.
- User selected moving SUTD users with one base station, two software UEs and live traffic. The existing Spark capacity-runner setup is present; two old dashboard processes remain and will be preserved.
- Confirmed Spark SSH access, a sampled GB10 GPU utilization of 0%, and four existing running containers. Reviewed the available demo launchers and requested the user’s demo choice before launch.

### Spark presentation benchmarks — 2026-11-03

- Rounded the three final-table values above 99% to “~100%” as requested. Exact values and the strict real-time qualification remain in notes; version 80 was visually checked.
- Removed the final table’s strict real-time result column at the user’s request and expanded the two speed columns. The percentage values and 100% target remain; version 79 was visually checked.
- Replaced the final slide’s raw slots/s table with audited percentages of real-time speed and explicit FAIL verdicts. Defined the 100% target, retained raw rates and CPU IDs in notes, and visually verified the editable table at version 77.
- Added the official SUTD logo at the title page’s top-right corner, preserving its proportions. Saved version 75, cited the university’s asset in the notes, and visually checked placement.
- Replaced the title-page QR code and caption with the project README website. The page returns HTTP 200, and independent decoding of the source, 250-pixel image and saved slide confirms the exact requested URL.
- Made the title-page contributors line bold to match the project and steering leads. Saved version 56 and visually checked the line’s fit.
- Changed the title-page subtitle to “Report on Unified Memory Architectures.” Saved version 55 and visually verified that the text fits on one line.
- Updated the cover to “OCUDU GPU CHANNEL — Unified Memory Architectures,” removing the discrete GPU wording. Visually checked the single-line subtitle and preserved the five-slide content.
- Added a title page using the old deck’s final-page team, roles, contacts, project icon and original GitHub QR code. Reviewed the five-slide deck, preserved existing slide order and measurements, and updated page numbers and internal notes.
- Reworded page 4’s bottleneck sentence to “srsUE processing, radio transport and CPU scheduling limit UE scaling.” Saved the live deck at version 44 and visually checked the line’s fit.
- Clarified page 3’s visible subtitle as channel emulation only with synthetic IQ sources. Speaker notes explicitly exclude OCUDU/srsUE execution; the chart values and live-stack results remain unchanged.
- Verified all 949 exported evidence files against their hashes and confirmed the saved slide table matches the audited clock data. All owned radio processes and monitors are stopped; four pre-existing containers remain running.
- Completed 15 retained Spark cold runs and audited gNB/every-UE clocks ([evidence](slides/spark-20261103/capacity-20261104/RESULTS.md)). No tested count met 1000 slots/s; matched CPU-access controls identify substantial UE CPU pressure alongside radio transport/scheduling overhead.
- Replaced page 4’s expected limitation with measured all-radio rates, explicit failed real-time qualification and the next step of GPU physical-layer integration. Reviewed the native table and preserved the other three slides and their benchmark data.
- Completed ten fresh Spark runs at 1/2/4/8/16 UEs; original-allocation gNB progress falls sharply beyond four UEs. An unmatched-trace control was excluded; exact-input CPU-allocation controls, per-UE clock audits and a radio-response diagnostic remain active.
- Started the user-requested fresh Spark one-gNB capacity investigation at 20 MHz with CPU OCUDU and srsUE. Existing GPU-PHY results are historical context only; the new sweep will measure radio progress and per-component CPU work in isolated sessions.
- Added page 4 on OCUDU/srsUE CPU execution as Spark’s expected scaling bottleneck. The slide distinguishes synthetic channel latency from live-stack capacity and states that Spark’s UE/gNB limit remains unmeasured; layout and numbering were reviewed.
- Clarified page 3 with a wider chart, larger axis/legend text, explicit Spark copy-control labeling and one 64-stream takeaway. Reviewed the render and retained every measured point, CPU/GPU model, protocol detail and maximum-latency qualification.
- Removed pages 2 and 3, the discrete GPU architecture and synchronization diagrams, at the user’s request. Renumbered the three remaining slides: Spark architecture, matched benchmark setup and channel-processing latency results.
- Added page 2 for the workstation’s discrete GPU architecture, matching page 1’s CPU, memory and GPU positions. The five-slide deck shows separate device IQ buffers and PCIe copies; renders and page numbering were verified.
- Renamed the saved presentation to “OCUDU GPU Channel — Discrete GPU and Unified Memory Architectures” and verified the title by readback. The existing presentation identity and four slides are preserved.
- Removed slide 2, the combined memory explanation and IQ-flow diagram, at the user’s request. Renumbered the remaining four slides; architecture, synchronization, benchmark setup and matched latency results remain.
- Consolidated overlapping slides 2–3 into one unified memory architecture and IQ-flow explanation, preserving the 128 GB label, transport note and source qualifications. The five-slide deck retains the matched latency chart and consistent block placement.
- Aligned slides 1–3 to identical CPU-left, shared-memory-center and GPU-right regions, with matching input/output buffer positions and straight IQ paths. Reviewed all three renders and verified block geometry; the other slides and benchmark results are unchanged.
- Standardized architecture names across slide text, chart legends and speaker notes to “discrete GPU architecture” and “unified memory architecture.” Reviewed the affected renders; CPU/GPU model labels and benchmark values are preserved.
- Added Intel Core Ultra 9 285K alongside RTX 5090 in the benchmark setup, chart legend, callout and measurement caption. Reviewed both affected slide renders; raw measurements are unchanged.
- Removed slide 7, the TDL-A latency chart, at the user’s request. The six-slide presentation retains the shared-memory diagrams and stream-scaling chart; raw TDL-A benchmark evidence is preserved.
- Standardized all 21 Spark diagram connector segments to 3 px with matching large triangle arrowheads and consistent input/output label styling. All four rendered diagrams were reviewed; slide order and benchmark charts are unchanged.
- Matched arrows on the four Spark diagram slides to the reference deck: native 3–4 px connectors, large triangle heads, teal input and navy output. Cleared labels and corrected routing; reviewed all four renders without changing benchmark charts.
- Removed the Spark presentation’s title slide at the user’s request and renumbered the remaining seven slides. The deck now opens with the Spark system architecture; measured results are unchanged.
- Created a separate eight-slide Spark presentation with shared-memory diagrams and two editable charts matching the reference style. Every slide was rendered and reviewed; chart values match audited results, with saved-deck identifiers in `slides/spark-20261103/presentation.json`.
- Defined discrete GPU architecture and unified memory architecture in the vocabulary using NVIDIA’s terminology; recorded the requested platform-benchmark exception in the harness.
- Completed 48 matched Spark trials plus 16 warmups at the archived RTX benchmark revision. All 12 checks, 633 source hashes and result-hash audits passed; no foreign GPU client was observed. Evidence: `slides/spark-20261103/evidence/`.
- Built the archived RTX benchmark revision for Spark in an isolated container. Stopped the active demo process group at the user’s instruction; four existing containers remain running, and the temporary benchmark container and monitors have exited.

### Remaining OCUDU slot delay — 2026-11-03

- A 30-second isolated/FIFO trace captured 5.166/5.417/5.439 ms scheduler-entry gaps. The normal-priority gNB ZMQ I/O thread waited 4.157/2.893/1.537 ms runnable while PHY receive calls waited 4.865/5.158/5.133 ms for IQ.
- The first gap’s dominant 4.155 ms dispatch wait overlapped background manager/Python work; the second included 2.886 ms of competing radio polling. All 160,963 packets arrived, with zero overflow/gaps/ZMQ errors and 38 lifetime starvations remaining.
- Corrected executable-probe append mode and namespace-relative process discovery; two aborted starts are excluded. Restored host settings and removed owned probes/instances, preserving 14 other containers; pruned 1.28 GB while retaining 0.75 MB of selected traces.
- Added the [remaining-delay report](docs/reports/validation/live-runtime-limitations-20261104.md), direct-entry continuity, selected kernel events and causal timeline. Evidence checks passed; strict documentation validated 126 pages and 19,979 links without errors.

### Short OCUDU CPU separation — 2026-11-03

- A 60-second same-process FIFO comparison improved 984.237→992.230 slots/s after excluding CPU 0 and separating unbound kernel work. IRQ discovery read a container-masked file and was corrected before a separate 30-second follow-up.
- The follow-up verified all 13 gNB threads excluded CPU 0 and captured display work stayed on CPUs 0/22. It achieved 981.943 slots/s with all 160,963 packets delivered, zero detected overflow and remaining starvation; improvement is inconsistent.
- Restored original workqueue and requested/effective IRQ affinities, normal PHY priority, and stopped both diagnostic containers. Preserved 14 other running containers; pruned 15 completed-run files, freeing 2.85 GB while retaining under 1 MB of placement traces.
- Added the [CPU-separation report](docs/reports/validation/live-runtime-limitations-20261104.md), applied phases, independent SFN/IQ timing, placement evidence and restoration checks. Evidence checks passed; strict documentation validated 125 pages and 19,807 links without errors.

### OCUDU wake-up investigation — 2026-11-03

- Confirmed old benchmark logs occupy 167 GB and selected only completed runs from this investigation for cleanup. A dedicated tracing container can access isolated kernel tracing instances; host scheduling statistics remain disabled and global tracing settings are unchanged.
- Removed 205 old bulk log, packet-capture and trace files from completed timing runs, freeing 94.94 GB of disk blocks. Compact results, configurations, sources and CTest logs remain; deletion paths and sizes are recorded in the cleanup manifest.
- Reproduced three 1 µs sleeps lasting 5.664/9.723/6.224 ms: timers fired after 52–54 µs, then the runnable PHY thread waited 5.609/9.670/6.169 ms. OCUDU's sequential ZMQ source explicitly selects normal priority; the reversible test confirmed its impact.

- Renamed the local SSH key pair to `spark-hardware-in-loop` and updated SSH/project configuration references. Passwordless login through the existing `spark-17a4` alias passed.

- Reversible normal/FIFO/normal/FIFO phases measured 966.990/985.809/968.555/988.262 slots/s, with 461/0/606/1 sleeps exceeding 1 ms. All 963,222 packets arrived; 189 starvations remain, so real-time qualification still fails.
- Live FIFO tracing captured residual 6.28 ms kernel-worker delays; a separate 60-second probe directly identified Intel display power-resume `intel_dmc_load_program` inside `output_poll_execute`. Earlier unstacked live callback identity remains inferred.
- Completed cleanup freed another 12.07 GB, totaling 107.01 GB; retained kernel captures total 20.68 MB. Both diagnostic containers are stopped, the owned trace instance removed, and 14 other running containers preserved.
- Added the [wake-up report](docs/reports/validation/live-runtime-limitations-20261104.md), selected events, phase calculations, source evidence and cleanup manifests. Remote CTest passed 13/13; strict documentation passed 124 pages and 19,639 links without errors.

### Additional SSH connection — 2026-11-03

- Added an additional Spark SSH connection to ignored `.config`; passwordless authentication passed using the configured key. Existing remote defaults are preserved.

### Look-ahead and catch-up experiment — 2026-11-03

- Confirmed that production source remains at `c3135a9`; prior investigation documents are pending local edits. Preparing an isolated prototype and fresh matched baseline; the existing additional container remains active and will be preserved.
- Prepared an isolated opt-in prototype for 1–4 batch output bounds and catch-up debt, including request-gated dynamic debt. Default behavior and the 5 ms starvation threshold are retained; all 13 RTX tests pass.
- The first 60-second control measured 995.625 slots/s with no detected overflow, gaps or ZMQ errors. The sequential scout matrix is running under `validation/scheduler-20261013/margin-20261103/`; no production runtime files were changed.
- Short runs measured 995.685 slots/s with 2 ms look-ahead alone, 998.725 with 2 ms catch-up alone, and 999.319 with both. All retained starvation events; four-port gates and three 600-second comparisons are queued after the scout matrix.
- Defined look-ahead and catch-up margins in the vocabulary and public glossary. OCUDU source confirms requests do not check internal free buffer space, so request-gated experiments retain a catch-up cap; production defaults remain unchanged.
- Fixed 4 ms catch-up with 1 ms buffering measured 999.742 slots/s over 60 seconds, outperforming the dynamic request gate's 996.768. Selected fixed, dynamic and default settings for 600-second comparisons; matching four-port checks retain their fixed 20-second duration.
- Four-port controls attached, passed traffic and advanced every port without detected overflow. The initial matrix checker lacked NumPy; checking the preserved default capture found silent UL input, so independent live UL matrix qualification remains failed.
- The fixed-cap long run accumulated 1.38 seconds of lag in 96 observed seconds despite its strong short result. Started a read-only host-side observer; corrected the earlier report's process-observation scope because container observations cannot exclude external work.
- Completed fixed 4 ms catch-up over 600 seconds: 983.317 slots/s, 10.010 seconds lag, 2,175 starvations and nine missing packets among 3,229,424 scheduled. Overflow/gaps/ZMQ errors were zero; dynamic and default comparisons continue.
- Completed dynamic request-gated catch-up over 600 seconds: 970.055 slots/s, 17.967 seconds lag and all 3,229,424 packets received. Lifetime starvations were 2,113; overflow/gaps/ZMQ errors were zero, and the final default control is running.
- Completed the 600-second default control: 963.364 slots/s, 21.982 seconds lag and one missing packet. Fixed 4 ms catch-up recorded 54.5% less lag but higher bulk packet delay and nine missing packets; shared-host activity limits attribution.
- Collected the report, plot, patch and bounded evidence locally under `docs/reports/experiments/slot-margin-20261103/`. All five four-port traffic checks advanced without detected overflow; only the both-2-ms and dynamic captures passed the nonzero UL matrix check.
- Stopped the experiment container and host observers; thirteen original containers remain running. Another original container had already exited at 06:30:33 UTC before cleanup; no unrelated container was stopped or restarted by this experiment.
- Verified unchanged raw measurements for eleven runs, all six 13-test logs and 87 artifact hashes. RTX strict Sphinx and publication checks passed: 123 pages, 19,465 links and zero errors; the final figure is visually checked.

### Long-run live timing investigation — 2026-11-03

- Confirmed the RTX GPU is idle and no radio test processes are running; existing services remain active. Preparing an isolated current-source experiment using the previously provisioned, pinned OCUDU/srsUE native workspace.
- Started a 600-second moving-channel run with one OCUDU gNB, two srsUEs and the CUDA broker; all 13 build checks and initial connectivity passed. At 219 observed seconds, gNB progress is 997.815 slots/s with no overflow or sample gaps.
- Completed the 600-second baseline: 997.472 slots/s and 1.517 seconds of radio lag; all 3,229,424 packets arrived. Broker lifetime counters report 72 input starvations and zero overflow/gap/ZMQ errors; the 300-second traced diagnostic is starting.
- Excluded diagnostic attempt `20261103T052247Z`: observer-preloaded Sionna crashed before IQ or traffic; the broker processed zero samples. Confined the observer to broker/gNB/srsUE processes and restarted, preserving failed artifacts.
- Excluded startup attempt `20261103T052439Z`: the linker dropped the observer's ZMQ dependency, causing srsUE constructor exit 119. Explicit linking changes the startup probe from 119 to zero; payload checks pass and a fresh diagnostic is starting.
- Completed the traced run: exact pacing accounting explains 1.993 seconds of lag, with 98.423% of discarded catch-up time in wait-dominated iterations. Resolved a 1 µs PHY-worker sleep lasting 10.512 ms; the initiating OS/driver cause remains open.
- Added the long-run timing report, plot, bounded measurement summaries and reproducible observers; production runtime code is unchanged. RTX strict Sphinx and publication checks passed: 122 pages, 19,041 links and zero errors.
- Defined underflow alongside starvation and overflow in the vocabulary and public glossary, linking the measured distinction. Restored the validation-index link to the existing profile-activation report after strict Sphinx detected its orphaned page.
- Stopped the dedicated experiment container and verified that all 13 pre-existing containers remain active; another container was present at cleanup. Raw runtime logs, traces and failed observer attempts are preserved on RTX.

- Prepared the validated profile-activation fix, regression coverage and validation report for the user-requested commit and push to main. RTX CPU/CUDA suites passed 13/13 each and GPU sequence 9/9; no implementation changes followed validation.

- Consumed pending profile replacements after activation, preventing unrelated updates from resetting delayed samples. The regression failed before the fix; RTX CPU/CUDA suites passed 13/13 each and GPU sequence 9/9; see the [report](docs/reports/validation/profile-activation-20261103.md).

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

- `validation/runtime-cleanup-20261104/`: hash-verified archive of the five timing bundles and temporary helpers, compact limitation evidence, and cleanup checks; local backup is `.git/cleanup-backups/runtime-20261104/`.

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

The SUTD demo changes are uncommitted. Share the complete `validation/sutd-crosstalk-handoff/PROMPT.md` text; it embeds the code and clean-Spark commands. A ten-minute viewing run is active at local port 18080; check the recorded sessions before restarting or cleaning its logs.

Real-radio experiments remain stopped; only the requested synthetic viewing run is active. RTX causal findings and unresolved Spark periodic gaps are recorded; CUDA-enabled checks pass 13/13 on RTX and 12/13 on Spark, where the old CPU-reference equality failure persists.

Open problems #11–#19 are tracked in [GitHub-linked issue records](docs/issues/README.md). Issue #20 is closed with its fix and verification linked to published commit `fd0e61a`; no further runtime experiment is queued.

The earlier Spark demo is stopped (`gate_exit=1`), and its local tunnel PID is inactive. Pedestrian coverage failure and timing misses remain recorded in `validation/spark-live-demo-20261104/`.
