# Merge Sionna RT contribution and validate on RTX 5090

> Historical record; its claims apply to the original date, revision and setup. Closed or historical design: consult current reference and validation before treating a proposal as implemented. Migrated from `docs/plans/merge-miso-siso-sionna-rt-5090.md` at `58d3156` without changing recorded measurements.


Historical plan, inspected 2026-09-13. Integration and validation were subsequently performed; the user authorized consolidation into local main on 2026-09-14. See the [current guide](../milestones/sionna-integration-20260914.md) and [merge report](../../reports/validation/main-merge-validation.md) for implemented behavior, results and remaining failures. The plan below records the original decisions, not current task status.

## Pinned inputs and findings

| Input | Inspected revision |
| --- | --- |
| Destination, `zhouyou-gu/ocudu-gpu-channel:main` | `f51c3fdaa830e56bfc5384cabd240022f507717a` |
| Contribution, `ehs0/ocudu-gpu-channel:miso-siso-sionna-rt` | `066a702ab2ecc301d5329c5fec89baefa30336f3` |
| Common ancestor | `d00e84ea2bc81f3af8108fce351d972a79a3d4c2` |
| Existing local checkout | `ehs0-sionna-rt-integration`, `5f55bb7` |

Main has 9 unique commits; the contribution has 25. Its change set from the common ancestor touches 317 files, including many scene assets. Main already contains the rank-1 MISO/SIMO implementation and the four-UE attach fix. The contribution extends that work with dynamic matrix profiles, Sionna, native launchers, telemetry, Web UI, and scene tools.

`git merge-tree --write-tree` predicts conflicts in `AGENT_PROGRESS.md` and `scripts/remote/ocudu-multi-gnb-smoke.sh`. Cleanly merged files still need semantic review.

The current checkout has pre-existing edits in `scripts/remote/ocudu-multi-gnb-smoke.sh` (demo hold/keepalive) and `apps/dashboard/index.html` (English text). Preserve them in this checkout; assess their intent separately when preparing the integration branch. Do not copy the older UI file over the new viewer. The local commits `b726ab3` and `5f55bb7` also need an equivalence check: main contains subnet-selection work, while bridge argument forwarding must remain available.

### RTX 5090 inspection

The machine configured by ignored `.config` was reached through `scripts/remote/common.sh`:

- RTX 5090, driver `580.173.02`, reported memory `32607 MiB`; CUDA toolkit `12.8.1` (`nvcc V12.8.93`), CMake `3.31.12`, and Compute Sanitizer are available.
- Linux `6.8.0-137-lowlatency`; virtualization probe reports `none`. Older LXD notes belong to a different environment.
- Python `3.12.3`; the existing Sionna environment reports `sionna-rt 2.0.1`, Mitsuba `3.8.0`, and Dr.Jit `1.3.1`. `libnvoptix.so.1` is visible. Package presence is not a successful ray-tracing test.
- Docker Compose `v5.0.1` is available. Open5GS services and other jobs are running. Do not stop or replace them for validation.
- `/dev/net/tun` exists, but `unshare --user --map-root-user --net --mount --fork /bin/true` fails with `uid_map: Operation not permitted`. Native launchers are currently blocked. No native workspace was found at the three inspected conventional locations; a nonstandard location remains possible.
- The existing remote project is a dirty rsync mirror whose Git status does not identify the source it currently contains. Do not use its Git HEAD as baseline evidence, or run the existing `rsync --delete` helpers against it for this merge.

The RTX 5090 target is SM 12.0; use the project's `OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120` setting. NVIDIA lists the device's compute capability as 12.0 in its [GPU table](https://developer.nvidia.com/cuda/gpus). Keep the installed toolchain for the first comparison; a driver/toolkit upgrade would introduce another variable.

## Merge method: preserve the original graph

Use an integration branch based on pinned main and a normal merge commit. Do not squash, rebase the contribution, cherry-pick its commits, or force-push main. This retains original commit IDs, authorship, and merge ancestry. Record fixes as additional commits with their actual authors.

Commands below describe the later execution, in Bash from the canonical repository. Fetch and verify revisions again first; if either changed, update the comparison and plan before proceeding.

```bash
git fetch origin main
git fetch ehs0 miso-siso-sionna-rt
test "$(git rev-parse origin/main)" = f51c3fdaa830e56bfc5384cabd240022f507717a
test "$(git rev-parse FETCH_HEAD)" = 066a702ab2ecc301d5329c5fec89baefa30336f3

# Preserve the pins with local refs, then leave the dirty checkout in place.
git branch safety/main-before-sionna-20260913 f51c3fdaa830e56bfc5384cabd240022f507717a
git branch review/ehs0-sionna-20260913 066a702ab2ecc301d5329c5fec89baefa30336f3
git worktree add --detach ../ocudu-main-baseline safety/main-before-sionna-20260913
git worktree add -b integration/sionna-rt-5090 ../ocudu-sionna-integration safety/main-before-sionna-20260913

cd ../ocudu-sionna-integration
git merge --no-ff --no-commit review/ehs0-sionna-20260913
# Resolve, inspect the staged result, run cheap checks, then commit the merge.
```

Resolve the launcher by combining main's subnet selection, matching Open5GS addresses, and current srsUE source with the contribution's optional core port mapping, per-antenna endpoints, and Sionna launch path. Pay particular attention to quoted versus expanding YAML heredocs: a syntactically valid shell script can still emit literal `${...}` or wrong addresses. Check the resolved Compose configuration before starting containers. Confirm CIDR overlap, not just identical subnet strings. Keep the existing static test mode usable.

Resolve progress records by preserving main's current state and archived ledger, incorporating contribution records with their original host/revision context, and recording this merge separately. Do not replace main's history with the stale local progress file. Preserve main's README credits, four-UE fixes, rank-1 documentation, and Pages configuration; add appropriate credit for the Sionna contribution when its scope is documented.

After the merge and after every final integration update, require both checks:

```bash
git merge-base --is-ancestor f51c3fdaa830e56bfc5384cabd240022f507717a HEAD
git merge-base --is-ancestor 066a702ab2ecc301d5329c5fec89baefa30336f3 HEAD
git log --graph --oneline --decorate -35
```

## Review gates before hardware acceptance

1. **Delay bounds and CPU/CUDA agreement.** The new matrix parser accepts delays through 1023 samples; `device_channel.h` allocates a 128-sample device history, and `refresh_all_taps_from_live()` caps the resized history at 128. Resolve this mismatch before acceptance: reject unsupported profiles before publication or implement a validated larger buffer. Exercise `ceil(delay) + kTdlFracFilterTaps` at, below, and above the actual bound, including small processing batches and cross-batch history. An ACK must never conceal unsupported delay behavior.
2. **History and timing semantics.** `run_bridge.py` retraces wall-clock positions and requests one coefficient time sample; it sends `matrix_profile_swap` on each update. Activations clear lane histories and enter warmup. Main's [existing Sionna plan](https://github.com/zhouyou-gu/ocudu-gpu-channel/blob/f51c3fdaa830e56bfc5384cabd240022f507717a/docs/plans/sionna-live-channel.md) instead specifies buffered geometry epochs and coefficient updates on the absolute IQ timeline. Preserve that distinction. A possible interim scope is an opt-in snapshot mode with measured reset effects; it does not establish continuous Doppler, sample-aligned replay, or the buffered-streaming milestone. The user requested an explanation of these alternatives and has not selected a scope. Measure replacement transients at G6 before deciding whether the limited mode is acceptable or a separate continuity implementation is required before merge. Existing buffered-streaming requirements remain unchanged.
3. **Dynamic matrix coverage.** The newly added distinct-lane matrix processing test uses the CPU backend. Add CUDA parity tests for dynamic 1x1, 2x1, 1x2, 4x1, and 1x4 updates, unequal lane delays, phase cancellation, and changing tap counts. Existing static matrix tests do not prove the new update path. Verify fallback behavior explicitly.
4. **Control concurrency and atomicity.** Test invalid dimensions, missing/duplicate lanes, duplicate delays, non-finite and oversized numeric input, oversized messages, `fixed_mimo` rejection, abort, and scalar/matrix transitions. A failed batch must leave all live profiles unchanged. Stress publication while the processor snapshots profiles; verify that no lane observes a partially updated matrix. Same numerical slot indices across links must not be described as a global sample barrier.
5. **Test and packaging completeness.** With Python discovered, the contribution registers 11 CTest entries, including the rank-1 renderer. OSM tests are outside CTest: run full Python discovery as well. Inventory renderer/verifier self-tests. Review the root `quickjs-1.19.4.tar.gz` for intended purpose before including a generated dependency archive. Retain scene ODbL attribution and vendored three.js notices. Check a clean offline scene/viewer load.
6. **Existing gate strength.** Review changes to `verify-legacy-1x1-artifacts.py`; socket readiness is not equivalent to proving the expected radio-node topology. Preserve or replace each removed invariant with equally strong evidence. Recheck shared native bootstrap changes against the locked OCUDU, Open5GS, and srsUE revisions.

## Ordered validation matrix

Run the original main baseline first, then the candidate under the same hardware, source pins, scenarios, traffic, and environment. Runs that compete for the GPU or fixed radio ports are sequential. Record a baseline failure before diagnosing a candidate regression; do not repeatedly retry a flaky gate until one pass appears.

| Gate | Work | Acceptance and evidence |
| --- | --- | --- |
| G0: isolation | Save source pins, dirty-worktree patch, environment manifest, GPU processes, CPU governor/affinity, container/network/port inventory. Create separate baseline/candidate source, build, config, and result directories. | Existing jobs survive unchanged. Every artifact identifies the tested source. |
| G1: local checks | Fresh CPU Release configure/build/CTest for main and candidate; Python discovery; renderer/verifier self-tests; Bash syntax; diff whitespace. Add the missing behavior tests above. | All expected tests discovered and passed. Record counts and skip reasons. CPU success is not GPU validation. |
| G2: native CUDA | Fresh Release build on 5090 with CUDA explicitly selected and architecture 120. Run full CTest and hardware probe. | CUDA compiled and runtime available; intended kernels actually execute; no silent CPU fallback. All numerical checks pass. |
| G3: memory and thread safety | Compute Sanitizer memcheck and initcheck on processing, runtime updates, and short dynamic-matrix stress; add synccheck where the kernels use synchronization. Use a separate CPU sanitizer build for control publication/races. | No sanitizer findings. These slow runs are correctness tests, never latency measurements. |
| G4: established GPU regression | Run `scripts/remote/gpu-test-sequence.sh` from each isolated source: build, CTest, clean relay, AWGN relay, 3-node graph, 2-cell graph, TDL-A. Extend to TDL-B/C/D/E and main's matrix/superposition fixtures where not covered. | All seven existing steps pass; strict relay starvation, overflow, sequence-gap, and ZMQ-error counters are zero; expected power and ordering preserved. |
| G5: Sionna correctness | Real GPU solve: simple static LOS, multipath, outage, separate FDD DL/UL, then supplied SUTD scenarios. Use impulse/tone inputs and independently calculated per-lane sums. | Correct units, absolute delay, complex phase, RX/TX order, polarization/port count, and coherent sum. No double path loss or fading. Quantization/pruning/clipping and any -100 dB outage floor are reported. |
| G6: update behavior | Static baseline, one replacement, then repeated updates. Correlate control ACK, sequence number observed by the backend, warmup completion, and actual IQ output. Stop/restart the bridge and broker; slow/stall the producer and subscriber. | ACK is never presented as applied IQ. No partial lanes, stale sessions, unbounded queue growth, or hidden source loss. Last-profile hold or failure behavior is explicit. Reset artifacts are measured against a reference. |
| G7: live radio regression | Re-run Milestones A, B (including main's four UEs), C, and documented rank-1 combinations: 2T2R with 1/2/3/4 UEs and 4T4R with 1/2/4 UEs. Inventory additional existing rank-1, transport, oracle-precoding, and verifier gates from main's evidence manifest. | Every intended UE obtains RRC, PDU, distinct identity/IP, and working user-plane traffic. Wire captures prove the configured matrix and contributions of every active source. Keep each existing verifier's numerical tolerance. |
| G8: Sionna live radio | Progress from SISO, to 2T2R and 4T4R single-UE dynamic channels, multi-UE, then two gNBs with inter-cell links. Use simple geometry before SUTD routes. | Correct serving cell and per-port routing, sustained traffic, backend application evidence for every expected physical link, and no missing source hidden by attach-only checks. Live claims remain rank-1 with single-port srsUE. |
| G9: UI and scene tools | Real browser check via SSH forwarding: 3D assets, per-cell/crosstalk colors, routes, matrix dimensions, tap values, KPI ownership/sentinels, stale/disconnected states, and restart. Exercise `measure_cell_coverage.py` and offline scene rebuild in scratch output. | No JS/HTTP errors or false green status; dashboard values match raw telemetry. Default scene geometry and paths match the supplied assets. Coverage predictions are distinguished from radio attach evidence. |
| G10: performance, soak, packaging | Compare native and containerized broker; UI off/on; static and moving scenes. Sweep requested Sionna rates 2, 10, 50, 100, 500 Hz after lower rates pass. Three independent startup cycles per accepted topology, then a 30-minute soak of the most demanding accepted Sionna case. | Record achieved update rate and lag, process/nominal-slot p50/p95/p99/max where available, misses, traffic losses, memory growth, GPU load, and warmup fraction. Clean teardown and restart. No unsupported rate advertised as achieved. |

For G7, reconcile main's README aggregate of eight live gates with its seven enumerated antenna/UE combinations using the stored evidence; do not invent an additional case. Existing native rank-1 runners fix their test duration to 20 seconds. Keep that gate intact and build a separately scored soak; increasing an environment variable is not automatically supported.

### Numerical and timing rules

- For CPU/CUDA deterministic fixtures, retain the project's established 1e-3 tolerance unless an existing verifier requires tighter bounds. Use analytic `y_r[n] = sum_t sum_k h_rt,k x_t[n-d_rt,k]` fixtures with independent lane inputs so swapped or missing ports cannot pass.
- Compare Sionna fractional delays using declared bandwidth, lag window, and error metric: its ideal sinc response differs from the broker's finite 8-tap interpolation. Do not demand bit-exact equality between different filters.
- Real-time budgets must match the samples and sample rate actually processed. A whole 1 ms batch and an SCS-based 500 microsecond benchmark budget are different configurations. Distinguish measured call timing from the telemetry's sample-proportional nominal-slot estimates. Report transport/scheduling delay separately from kernel time.
- Strict realtime runs require zero misses/errors in the scored interval. Live attach alone is a functional gate; any nonzero starvation or deadline misses remain visible and prevent a strict-realtime claim. Do not relax acceptance thresholds to make the candidate green.
- A proposed comparative performance gate is no more than 10% regression in median-of-three p99 measurements under matched conditions, alongside the absolute configured deadline. If baseline variability is larger, stabilize the environment and measure again before setting a conclusion. This threshold is a plan choice, not an existing measured property.
- Start moving-channel tests at 2 or 10 Hz. The bridge and remote Sionna smokes default to 500 Hz, while the native single-UE launcher defaults to 10 Hz. Neither setting proves that the scene solves at that rate. At 500 Hz, measure GPU contention with IQ processing and the effect of clearing channel history at each accepted update.

## Execution details on the 5090 PC

Use a dedicated merge-test workspace. For remote shell helpers, create an ignored `.config` inside the integration worktree with candidate-only project/build/result paths; `common.sh` reads that file and can override environment settings. Do not point `gpu-test-sequence.sh` at the existing mirror: it uses `rsync --delete`. Prefer a real Git checkout or bundle of the candidate on the PC for native runners that use `git ls-files` to produce source manifests. Verify tree/file hashes even when copying files rather than checking out a commit.

Keep downloads, venvs, builds, captures, and raw results outside tracked source. Freeze the successful Python environment, including transitive packages, and record OCUDU/Open5GS/srsUE commits and container image digests. Floating `master` or image tags are not reproducible evidence. The already-approved four-UE srsUE fix must be preserved; separately honor the exact native gate lock rather than silently substituting a different revision.

Example commands from an isolated source directory on the PC, with `CUDACXX` set to the inspected compiler and `SIONNA_PYTHON` set to an isolated/verified Sionna interpreter:

```bash
cmake -S . -B build-cuda-5090 -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="$CUDACXX" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120
cmake --build build-cuda-5090 -j4
ctest --test-dir build-cuda-5090 -N
ctest --test-dir build-cuda-5090 --output-on-failure
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/native/render-sionna-rank1-configs.py --self-test
compute-sanitizer --tool memcheck --error-exitcode 99 ./build-cuda-5090/test_processing
compute-sanitizer --tool memcheck --error-exitcode 99 ./build-cuda-5090/test_runtime_update_parity

"$SIONNA_PYTHON" apps/sionna_bridge/run_bridge.py \
  --scenario-config use_cases/configs/sionna/scenarios/simple_street/ocudu-rank1.json \
  --dry-run --iterations 3 --update-hz 2
```

Choose build concurrency after checking available host resources. A successful dry-run traces and serializes channels; it does not prove broker application.

Native launchers cannot be scored green on the currently inspected namespace configuration. Resolve namespace availability and locate/provision a compatible locked native workspace before running `run-ocudu-sionna-1x1.sh`, `run-ocudu-sionna-rank1.sh`, and `run-ocudu-sionna-multi-ue.sh`. Do not weaken or bypass their checks. If a host policy change is required, obtain administrator direction separately. In the meantime, G1–G6 and isolated Docker routes can proceed; a Docker result does not validate the native launcher.

Before Docker live gates, inspect the fully rendered Compose file and cleanup paths. Use unique project/container names, ports, and nonoverlapping subnets, and ensure teardown removes only this run's resources. Existing scripts contain fixed-name cleanup and cannot be presumed isolated merely because the source directory differs. Preserve active Open5GS and research jobs.

Use `scripts/tools/check_feed.py` with `--links` derived from the actual topology: its default list expects ten particular links and is wrong for a single-cell scenario. It checks feed presence/parsing; additionally verify seqno application, warmup completion, frame age, and IQ effects.

## Final acceptance, history checks, and rollback

Store one report row per gate with baseline/candidate commit, configuration hashes, dependency versions, GPU/host metadata, duration, result, and log/capture locations. Preserve failed attempts. Mark unavailable environments as blocked and unexecuted tests as not run. Proposed scheduling is one session for integration/local checks, one for GPU correctness and synthetic checks, and one for live matrices/soak; native provisioning and fixes can extend this substantially.

Main is ready to advance only when required correctness, regression, supported deployment, and live-radio gates pass, and the snapshot-versus-buffered scope is settled. Freeze the candidate commit before final validation. If main moves, merge its new tip into the integration branch, retain both ancestries, and repeat affected checks on the resulting commit; do not resolve new conflicts only in GitHub after testing an older tree.

Publish through a PR configured for **Create a merge commit**, or fast-forward main to the tested integration commit when allowed. Both preserve the contribution's original commits; GitHub squash and rebase merges do not. Verify both pinned ancestors again on the published `origin/main`, and link the test evidence in the PR. Re-run a short smoke on the published source tree.

Retain the previous known-good binary/image/configs and the local safety ref until post-merge verification is complete. To undo a published merge, revert the appropriate merge commit with the mainline parent identified from `git show --pretty=raw`; do not reset or force-push shared main. A revert keeps the contributor history visible.

## Evidence collected while making this plan

- Fetched the two requested branch tips and inspected their history and changed source.
- Computed the merge preview without changing the checkout or index; two conflicts were reported.
- Exported the contribution into a temporary directory and ran all discovered Python tests: **70 passed**. Its rank-1 renderer self-test also passed.
- Inspected the configured RTX 5090 host and ran the minimal namespace probe. Native namespace creation failed as recorded above.
- CUDA compilation, C++ tests, dynamic matrix GPU correctness, browser behavior, live radio, and performance remain untested for the combined candidate. The preliminary checks do not establish merge readiness.

Source references: [contribution snapshot](https://github.com/ehs0/ocudu-gpu-channel/tree/066a702ab2ecc301d5329c5fec89baefa30336f3), [main snapshot](https://github.com/zhouyou-gu/ocudu-gpu-channel/tree/f51c3fdaa830e56bfc5384cabd240022f507717a). Inspect the pinned source when documentation disagrees with implementation.
