# Sionna integration fixes and RTX 5090 validation

> Dated evidence, preserved from `docs/sionna-merge-fixes-validation.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../getting_started/status.md).


This is a dated validation record. See the [current integration guide](../../history/milestones/sionna-integration-20260914.md) for subsequent channel, dashboard and UE recovery changes; the results below remain tied to their recorded revisions.

The four bounded fixes pass regression checks. The stationary two-gNB/two-UE case passes connectivity, but moving SUTD connectivity and strict real-time qualification fail. Keep the changes on the integration branch; this is not an all-clear for main.

## Scope and history

Reviewed baseline: `88cbe6d34046e20a4fab277deb85166876d71139`.
Fixed executable source: `16af288050750932ab5f7d48fb6cca1f14a7c9d3` (C++ last changed in `b621b12`).
Normal integration merge: `3f4c234`, retaining contributor tip `066a702` as a parent and ancestor. No contributor commits were squashed or rebased. Original uncommitted edits remain in the original checkout; implementation uses the separate `integration/sionna-history-fix` worktree.

| Commit | Change |
|---|---|
| `260e8c6` | Shared 1023-sample control limit, 1031-sample CUDA storage, explicit capacity checks and delayed-echo tests |
| `66173e2` | Reject dynamic matrices on CUDA host staging before shadow/sequence mutations |
| `549803a` | Correct SUTD antenna topology, launch all endpoints, check consistency |
| `576def5` | Two-second monotonic telemetry freshness, stale labels, authoritative backend warmup |
| `b621b12` | Scalar maximum-delay coverage and preservation of static fallback preparation |
| `16af288` | Clear readiness when the dashboard HTTP service becomes unavailable |

History preservation from `88cbe6d` remains: identical and coefficient-only matrix updates retain samples and existing warmup state; actual delay/layout/settings changes reset. Scalar profile reset semantics and wire formats are unchanged.

## Environment and evidence

RTX 5090 workstation: Intel Core Ultra 9 285K, 24 logical CPUs, CUDA 12.8.1, NVIDIA driver 580.173.02. All remaining tests, including CPU reference comparisons, run on this workstation following the user's instruction. Earlier local test results are historical and are not substituted for remote results.

Remote artifact root: `~/ocudu-gpu-channel-workspace/validation/sionna-history-20260913/fix-validation/`. Baseline source/build remain isolated in its parent directory. Local raw review artifacts are ignored under `build-review-20260914/` in the integration worktree.

## Targeted and full regressions

- New delayed-echo regression fails against the reviewed GPU library with `long-delay CUDA echo must match CPU`, then passes with the fixed library (`delay-before-gpu.log`, `final-gpu-tests.log`). The earlier review probe separately demonstrated silent host-fallback matrix misprocessing.
- The original shipped SUTD launcher failed with matrix dimensions `1x4` versus prepared `1x1`; the corrected launcher ran directly on RTX 5090 for 90 seconds. It observed 1,214 Sionna batches, 12,140 link updates, nonzero traffic, and zero starvation/overflow/sequence-gap/ZMQ/control-rejection/telemetry-drop counters. Requested update and telemetry rate were 500 Hz; achieved Sionna update rate was about 13.5 Hz.
- Dashboard freshness regression fails before the change at the stale-readiness assertion, then passes. Controlled-clock tests cover exact two-second boundaries, missing timestamps, partial-link staleness, recovery, backend zero warmup and actual reset events.
- RTX 5090 CTest: 12/12. Python: 75/75. CPU reference and CUDA comparisons cover SISO, 2x1/1x2 and 4x1/1x4, independent input antennas, 8-sample batches, coefficient updates and cross-batch echoes at 5, 120, 120.5, 127.5, 128, 128.5, 129, 160, 1022.5 and 1023 samples, using the existing `1e-4` maximum-error tolerance.
- Compute Sanitizer memcheck, initcheck and synccheck: zero errors on the extended matrix/scalar history executable, including short and maximum delays (`sanitizers.log`).
- The final-source nine-stage GPU regression passed (`final-gpu-sequence.log`), including CTest 12/12. Final remote Python discovery passed 75/75. Shell syntax, rendered antenna endpoint mappings, source checksum comparison and Git whitespace checks passed.

## Sustained comparison and browser validation

The paired runs use the same SUTD topology, 23.04 MS/s, 23,040-sample batches, 100 Hz telemetry and 2 Hz Sionna updates, sequentially on the same host. Each records 601 observations spanning at least 600 seconds. The first baseline attempt overlapped compilation and is excluded; only `baseline-clean` is comparable to `fixed`. No build, additional test workload or scheduling changes run during the paired windows.

Both windows completed with 601 samples and zero collection errors. All ten link sequences advanced monotonically. These are synthetic broker traffic sessions, not OCUDU attach evidence. **Both runs fail strict zero-miss real-time qualification.**

| Revision | Receiver | Completed slots in window | Misses in window | p95 / p99 / max (µs) |
|---|---|---:|---:|---|
| Reviewed | gnb0 | 546,294 | 86 | 246 / 403 / 2,534.15 |
| Reviewed | gnb1 | 546,240 | 79 | 242 / 402 / 6,487.28 |
| Reviewed | ue0 | 546,334 | 179 | 311 / 497 / 7,092.31 |
| Reviewed | ue1 | 546,317 | 180 | 310 / 501 / 4,530.88 |
| Fixed | gnb0 | 545,027 | 72 | 245 / 397 / 3,098.45 |
| Fixed | gnb1 | 545,030 | 72 | 244 / 397 / 6,538.80 |
| Fixed | ue0 | 545,083 | 117 | 308 / 480 / 3,440.57 |
| Fixed | ue1 | 545,076 | 143 | 307 / 485 / 3,244.95 |

Counts are last-minus-first observations. Percentiles/max are the final cumulative backend histogram, including startup, with 1 µs percentile resolution. The metric estimates nominal 1 ms processing slots from call time and excludes transport waiting; it is not end-to-end radio latency. One pair does not establish a statistically significant performance improvement.

| Metric | Reviewed p50 / p95 / max (ms) | Fixed p50 / p95 / max (ms) |
|---|---|---|
| Channel generation | 69.52 / 78.20 / 87.22 | 70.47 / 78.12 / 97.95 |
| Control transaction | 1.68 / 3.71 / 5.29 | 1.62 / 3.69 / 7.02 |
| Generation to control ACK | 71.52 / 80.29 / 88.59 | 72.55 / 80.18 / 101.91 |
| Total update | 71.73 / 80.45 / 88.69 | 72.65 / 80.28 / 101.98 |

Update metrics are sampled dashboard observations, not a census of every Sionna iteration. Maximum sampled GPU memory in both runs was 516 MiB for the broker and 1,774 MiB for Sionna. No host scheduling settings or thresholds were changed.

Chrome has exercised the SUTD scene, ray/label toggles, view reset, antenna filtering and tap plots. Remote Chrome additionally verified that HTTP service loss displays “Backend status unavailable” and removes CUDA READY within three seconds, retaining control ACKs. API evidence confirms Sionna-only shutdown leaves the channel usable, broker shutdown yields ten stale links after 2.7 seconds with zero warmup links, and restarting the broker/bridge and synthetic clients restores ten applied/usable links. Old synthetic REQ clients needed restarting after their server disappeared; broker restart alone did not resume their traffic. `tests/test_web_ui_disconnect.mjs` also passes inside the existing `node:22` image on the RTX 5090 host.

## Actual OCUDU runtime

These tests used two real OCUDU gNB containers (four TX and four RX antenna ports each), two real srsUE containers (one TX/RX port each), Open5GS, the fixed CUDA broker, Sionna and two dashboard instances. OCUDU image source commit is `2563975`; exact image IDs are preserved in each run's `ran-core-images.json`, `srsue-image.json` and `runtime-containers.json`. The user’s other 11 core and two research containers remained running.

The validation harness is derived from `scripts/remote/ocudu-multi-gnb-smoke.sh`, with test-only changes: isolated Docker project/container names, reuse of installed images, supported scheduler metrics options, two WebSocket subscriptions and TERM shutdown of the background web wrapper. Its exact source is retained in `build-review-20260914/live-ocudu-inner.sh` and at the remote artifact root. This is an adapted live harness, not a claim that every supplied live launcher works unchanged.

The installed OCUDU rejects `metrics.layers.enable_sched_ue`. Its own `gnb metrics layers --help` confirms `enable_sched`; using that option enables per-UE reports on this version. Each dashboard instance accepts one gNB endpoint: port 19080 subscribes to gNB0 via loopback 18001, and port 19081 subscribes to gNB1 via 18002. The synthetic dashboard had no metrics endpoint and no actual gNB, which explains its empty RAN KPI panel.

| Live case | Result |
|---|---|
| Supplied moving SUTD (`20260914T033258Z`) | Both cells activated and both UEs reached RRC/PDU, but both camped on PCI 1. UE0 failed ping and lost connectivity; UE1 passed. An extra traffic check received 0/10 and 10/10 replies respectively. 3,763 broker starvations; zero overflow/gap/ZMQ errors. **Connectivity failure.** |
| Stationary SUTD control (`20260914T033611Z`) | UE0 on PCI 1 and UE1 on PCI 2; both RRC/PDU and live scheduler rows. Each received 250/250 ping replies at 0.2-second intervals with 0% loss. 56 starvations; zero overflow/gap/ZMQ errors and no gNB RF overflow. **Connectivity passes; strict runtime qualification fails.** |

The stationary case holds the nodes at positions observed in the paired trace at 99.5001 seconds: UE0 `[-84.6756, -37.7927, 1.5]`, UE1 `[-233.8647, -128.3907, 1.5]`. Serving tap gains were about -15.30 and -15.87 dB; both intercell paths were absent. It changes positions/motion only, not gain calibration, receiver noise, scheduling or thresholds. The generated scenario and selection evidence are retained as `stationary-sutd.json` and `stationary-selection.json`.

Stationary observations confirmed ten usable links, correct 1x4/4x1/1x1 dimensions, advancing profiles and only one warmup event per link despite repeated Sionna updates. Real container ownership, all published source ports, rendered gNB TX/RX endpoint mappings and both scheduler PCIs were checked. Source ports were 3000/3002/3004/3006, 3010/3012/3014/3016, 3101 and 3103; matching broker receive ports were 3001/3003/3005/3007, 3011/3013/3015/3017, 3100 and 3102. No synthetic source/sink processes participated in these live cases.

Remote Chrome verified scheduler rows for both PCIs, scene ray/label toggles, reset view, antenna-2 filtering, tap/frequency plots and resource cards. Browser screenshots and DOM evidence are retained under each live run’s log directory. CQI’s unreported sentinel remains an em dash; it is not fabricated into a measurement.

The inherited gate labels starvation as soft and returns `passed` for the stationary case. This review deliberately applies the stricter user/workspace qualification: 56 nonzero starvation events prevent a strict pass. Its 90-second option is an attach timeout, not a fixed elapsed duration; the stationary run additionally held the stack for 80 seconds and committed 208 Sionna batches. The moving run held for 60 seconds and committed 285 batches (including extra shutdown time).

The supplied SUTD route includes deliberate coverage outages and geometry changes. Results from that route must be separated from a stationary connectivity case; connected sockets alone cannot establish successful radio operation.

## Remaining limits

Geometry changes still reset channel history. Dynamic matrices on CUDA host staging are explicitly unsupported. The old static multi-gNB launcher default still selects a scalar topology despite generating four-port gNB configurations; that inherited path was not qualified by the Sionna tests. No seamless mobility, complete live-radio qualification or zero-miss real-time claim follows from the bounded fixes alone.


## Reproduction and cleanup

Load workstation settings through `scripts/remote/common.sh`. All commands below execute on the RTX 5090 host. Let `w=~/ocudu-gpu-channel-workspace`, `v=$w/validation/sionna-history-20260913/fix-validation`, and `p=$v/project`.

```bash
source "$w/tools/env.sh"
ctest --test-dir "$v/build" --output-on-failure
cd "$p"
"$w/venvs/sionna/bin/python" -m unittest discover -s tests -p 'test_*.py'
compute-sanitizer --tool memcheck "$v/build/test_matrix_profile_history"
compute-sanitizer --tool initcheck "$v/build/test_matrix_profile_history"
compute-sanitizer --tool synccheck "$v/build/test_matrix_profile_history"
docker run --rm --network none -v "$p:/work:ro" -w /work node:22 node tests/test_web_ui_disconnect.mjs
find scripts -name '*.sh' -print0 | xargs -0 -n1 bash -n
```

The exact remote body of `scripts/remote/gpu-test-sequence.sh` was extracted as `build-review-20260914/gpu-sequence-inner.sh` and invoked through `remote_sh bash -s -- "$w" "$p" "$v/suite-builds"`, with that file as stdin. This avoids overwriting the normal remote workspace. `run-baseline-clean.sh` and `run-fixed-comparison.sh` record all paired-run process arguments and collection code; `summarize-comparison.py` produces `comparison-summary.json`.

Stationary live invocation (replace the scenario with `examples/configs/sionna/scenarios/sutd/multi-gnb-sutd.json` and hold 80 with 60 for the moving case):

```bash
cd "$p"
bash "$v/live-ocudu-inner.sh" "$w" "$p" "$v/suite-builds" "$v/live-results"   "$w/ocudu" 90 0 master 8 __native__ sionna "$w/venvs/sionna/bin/python"   2 120 19080 "$v/stationary-sutd.json" "$w/tools/cuda-12.8.1/bin/nvcc"   local '' 80 600 0.2 examples/configs/topologies/sionna/topology.sionna-multi-gnb.cuda.yaml
```

Final cleanup stopped only recorded test processes, test containers and the test browser. No test listener or GPU compute process remained; all 13 unrelated containers remained running. Contributor tip `066a702` remains an ancestor. No squash, rebase, main merge or remote publication was performed.
