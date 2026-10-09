# Sionna integration and current validation

Status as of **14 September 2026**, published on `main`.
The broker accepts live Sionna matrix profiles, and one dashboard displays the
scene, channel delivery, GPU resources and independent scheduler metrics for
both gNBs. The bounded integration fixes pass their regressions. **Continuous
two-UE moving connectivity and strict zero-miss real-time qualification remain
failed.** The integration is published on `main`; no tagged release has been created.

## Contribution provenance

The [README contributor table](../README.md#contributors) is authoritative:
Zhouyou Gu (SUTD) is project lead, with steering by
[Jihong Park (SUTD)](https://www.sutd.edu.sg/profile/park-jihong/).
Minwoo Eun (Yonsei University) contributed the rank-1 MISO/SIMO workstream;
Hyunsoo Lee (Yonsei University, `@ehs0`) contributed the Sionna integration.
SUTD is the Singapore University of Technology and Design.

The normal merge `3f4c234` retains main `f51c3fd` and Hyunsoo Lee's contribution
tip `066a702ab2ecc301d5329c5fec89baefa30336f3` as parents. Original commit IDs
and author metadata remain intact. The contribution's Git author string
“Ubuntu” is retained in history; the documentation uses the contributor's
confirmed name, Hyunsoo Lee.

Local main merge `22a51ba` retains integration tip `0e0a6c3`. Follow-up normal
merge `e59e095` retains the older local tip `5f55bb7`, including subnet selection
and Sionna argument forwarding. No contribution was squashed or rebased. See
[merge validation and edit reconciliation](main-merge-validation.md).

| Work | Retained evidence |
|---|---|
| Minwoo Eun's multi-port radios, physical-link state, matrix channels and live gates | [Rank-1 feasibility report](rank1-feasibility-report.en.html) and [live gate results](live-gate-results.md); original `MinwooEun` commits remain in ancestry |
| Hyunsoo Lee's Sionna bridge and scalar/matrix integration | `048c947`, `6032d61`; original scheduler KPI panel `a418a86` |
| Hyunsoo Lee's moving SUTD scene, arrays, viewer and four-antenna scenario | `9722ef2`, `9309087`, `edd89ec`, `066a702` |
| Zhouyou Gu's integration corrections and validation | History fix `88cbe6d`; delay capacity `260e8c6`; fallback rejection `66173e2`; launcher correction `549803a`; freshness `576def5`; preparation coverage `b621b12`; HTTP-loss handling `16af288` |
| Zhouyou Gu's unified RAN panel and metrics recovery | `5c6cafd`, `d6637f7`; [validation report](metrics-recovery-validation.md) |
| Zhouyou Gu's UE recovery follow-up | Separate user-owned srsRAN fork, tested at `daa167ae3`; [results below](#ue-recovery-validation) |

These corrections build on the contributors' work; they do not replace its
history or imply that the Sionna contributor implemented the later UE fixes.

## Channel updates and limits

Sionna RT derives channel impulse responses from the scene and antenna arrays.
The bridge submits complete per-link matrices through the broker control
socket. IQ still flows between real radio endpoints through the CUDA broker;
the ray tracer does not process each IQ batch itself. Requested update rate
and achieved channel-generation rate are different measurements.

A matrix has `nr` receive rows and `nt` transmit columns. Its prepared antenna
dimensions are fixed; every `(rx_port, tx_port)` lane must be supplied.
SISO, 2×1/1×2 and 4×1/1×4 configurations are covered by numerical regressions.
The SUTD setup uses four antenna ports per gNB and one per UE, with eight
directed serving and intercell links. The scenes carry no UE-to-UE edge: the
cells are FDD, so a UE's uplink carrier is never heard by another UE's downlink
receiver, and the broker rejects such an edge when the ports carry carrier
labels (`tx_carrier`/`rx_carrier`).

| Incoming matrix profile | History and warmup |
|---|---|
| Identical profile, or only tap gain/phase changes | Preserve recent input samples and existing warmup state; update coefficients on CPU/CUDA |
| First profile, changed tap counts/ordered delays, or other channel settings | Reset lane history and start the existing warmup |
| Different antenna dimensions | Reject; dimensions must match the prepared link |

The preservation decision is made once per physical link and applied to every
antenna pair. Delays use exact equality, without a tolerance that could conceal
a geometry change. A preserving update neither restarts nor prematurely ends
warmup. Scalar `profile_swap` retains its existing reset semantics; the
matrix optimization does not change scalar control behavior.

Scalar and matrix control delays must be finite and within **0–1023 samples**.
CUDA reserves **1031 history samples per lane** at preparation: the shared
1023-sample bound plus the eight-sample fractional filter span. Construction
and refresh check capacity explicitly; excessive delays are not truncated.
CPU/CUDA agreement is numerical, at the relevant test tolerance (`1e-4` for
the delayed matrix comparisons), rather than a blanket bitwise guarantee.

Dynamic matrix updates on CUDA require the **device-channel route**. Links
processed through host staging reject them before shadow state or sequence
numbers change. Static mixed topologies retain their preparation behavior.
Sparse `fixed_mimo` links also reject dynamic matrices because all lanes must
be preallocated. Batch rejection/abort and subsequent recovery are tested.

This remains snapshot replacement at link update boundaries. It does not add
buffered, sample-aligned channel streaming or seamless geometry changes.
See [matrix history](sionna-matrix-history.md) and
[delay/fallback validation](sionna-merge-fixes-validation.md).

## Launcher arguments

The two-gNB smoke launcher retains `OCUDU_MGNB_SIONNA_EXTRA_ARGS`, for example
`--ue0-start=-20,0,1.5 --gain-offset-db 75`. Values are transported safely over
SSH, split on whitespace without shell evaluation or filename expansion, and
forwarded only to the Sionna bridge. To supply an individual argument containing
spaces, invoke `scripts/local/run_web_ui.sh` directly and put quoted bridge
arguments after `--`. An unset option keeps the existing defaults.

## One dashboard for both gNBs

Run one Web service with two named metrics subscriptions. This command starts
the Web service only; the broker, Sionna status writer and real gNB metrics
endpoints must already be running. Use the actual status JSONL path produced
by the chosen launcher.

```bash
python apps/dashboard/server.py \
  --bind 127.0.0.1 --port 19080 \
  --telemetry-endpoint tcp://127.0.0.1:5560 \
  --status-jsonl /path/to/run/sionna-status.jsonl \
  --gnb-metrics-source gnb0=ws://127.0.0.1:18001 \
  --gnb-metrics-source gnb1=ws://127.0.0.1:18002
```

Use the Sionna environment's Python interpreter. The existing
`--gnb-metrics-endpoint URL` single-source option remains supported and cannot
be combined with `--gnb-metrics-source`. The tested OCUDU build uses
`metrics.layers.enable_sched`, not `enable_sched_ue`, with JSON metrics and
remote control enabled. A Web subscription does not create radio traffic.

`GET /api/status` retains its existing fields and exposes:

| Field | Meaning |
|---|---|
| `ran_gnbs[gnb_id]` | Independent connection state, error, report counters, scheduler rows and receipt ages for each configured gNB |
| `ran` | Legacy view of the first configured gNB; preserves single-source clients |
| `ue_list_current_connection` | Present in each gNB and legacy view; true only after a scheduler report on the current connection |
| `ue_list_age_seconds`, `ue_list_stale` | Scheduler freshness, independent of heartbeat or other metrics messages |
| `feeds.telemetry_fresh_seconds`, `telemetry_age_seconds` | Two-second freshness bound and monotonic receipt age per channel link |
| `feeds.telemetry_link_count`, `telemetry_cached_link_count` | Fresh link count and retained cached link count, respectively |
| `delivery` | Whether the acknowledged channel profiles have fresh matching backend application and usable output |

The RAN panel stacks gNB0 and gNB1, identifying rows by gNB, PCI and RNTI.
Connection/handshake timeouts remain five seconds. After five seconds without
an incoming frame, the client sends a Ping and requires its matching Pong
within ten seconds. Closure, socket failure or invalid framing triggers
reconnection and resubscription; partial frames and fragments survive idle
waits. Heartbeats never refresh scheduler timestamps.

Scheduler reports older than five seconds are stale. On reconnect, cached
rows remain historical until a scheduler report arrives on that connection.
The UI distinguishes “Connected · scheduler data stale” from “Connected ·
waiting for scheduler report.” A fresh empty report clears only that gNB's
table. Neither RNTI reuse nor a returning row proves UE identity or a new
radio session.

Channel readiness separately requires fresh, applied and usable telemetry
for every required link. Missing timestamps are not fresh. Cached values and
control ACKs remain inspectable, but stale telemetry or HTTP service loss
removes `CUDA READY`; backend silence is visible within three seconds including
polling. Backend-reported zero warmup is authoritative, and staleness does not
create a reset interval. Stopping Sionna updates alone does not invalidate a
channel that the broker continues to apply and report.

## UE recovery validation

The default UE repository in the radio smoke scripts is
[`zhouyou-gu/srsRAN_4G`](https://github.com/zhouyou-gu/srsRAN_4G).
Recovery was tested on local branch `fix/sa-ra-recovery-5090`, source
**`daa167ae3443b046ce560df646c7dc5f17e5c1dd`**, reporting version `25.10.0`.
Those commits were not pushed during validation and are not claimed to be on
remote `master`; the default repository alone does not select this fixed build.

The fixes add deferred teardown/fresh registration after access failure or
network release, reset the synchronization trial budget, align workers and
PRACH with recovered slot timing, and route sustained synchronization loss
through the combined stack that srsUE actually instantiates in SA mode.
N310/N311/T310 use the existing SSB synchronizer indications. This is fresh
attachment, not full BLER-based radio-link monitoring, RRC re-establishment,
idle/resume support or seamless handover. Stack timers can take longer in
wall time when radio processing falls behind.

| Provenance | Value |
|---|---|
| Channel/Web | `b07df66`; CUDA executable from `16af288` (C++ last changed at `b621b12`) |
| OCUDU | `2563975a74baf94db51b4a41d6ff48e945cbb8f7` |
| UE image | `ocudu-gpu-channel/srsue-zmq:daa167ae3443b046ce560df646c7dc5f17e5c1dd` |
| UE executable SHA256 | `eba570b596d251bc8b7acba6126640d40bb53251ce9f88fe6c59deddba45d3e7` |
| Original moving scenario | `use_cases/configs/sionna/scenarios/sutd/multi-gnb-sutd.json`; SHA256 `9a67cd16d9aa7d06078598f7f5f48e74df0181bfdc0025448d8a241b02daf01c` |
| Radio workload | Two real OCUDU gNBs, two real srsUEs, Open5GS and ten CUDA links; PCI1/2, n3, 20 MHz, 23.04 MS/s; four ports per gNB, one per UE |
| Host | RTX 5090, Intel Core Ultra 9 285K, CUDA 12.8.1, driver 580.173.02 |

Run `20260914T071025Z` observed **600.049 seconds**, with 458 browser/API
samples. Both UE process identities stayed unchanged. A separate approximately
620-second periodic traffic capture recorded:

| Check | Result |
|---|---|
| UE0 periodic traffic | 58/58 batches with ten replies; 580/580 replies |
| UE1 periodic traffic | 19/58 batches with ten replies; 208/580 replies overall |
| UE1 automatic new sessions | Three returning gNB1/PCI2 sessions passed 10/10; the returning gNB0/PCI1 session failed with 1/10 |
| Final moving-window traffic | UE0 10/10; **UE1 0/10** |
| UE0 controlled 20-second signal loss, after the moving observation | T310 expiry, fresh RRC/PDU and 10/10 replies within **3.974 seconds** of restoration; no UE restart |
| Dashboard | Both metrics connections remained connected; stale scheduler intervals remained labelled; browser recovered without reload |

Returning sessions were correlated using individual UE radio logs, fresh
gNB/PCI/RNTI scheduler records and traffic. The route includes positions with
no traced paths to either gNB, but coverage has not been isolated as the cause
of every failure. Later successful pings do not erase the failed window.

The source report is `docs/sa-sync-recovery-followup.md` in the UE fork at
report commit `cb58e60858e3b9a02f9e70ecdf7ce9c558314b6d`; this section provides
the portable result summary without depending on an unpublished GitHub URL
or a workstation-specific file link. Raw evidence is under the validation
workspace's `validation/ue-sa-recovery-20260914/sync-loss-live/` directory.

## Regression record and qualification limits

The local merge adds launcher regression checks on the RTX 5090 host. Earlier
channel, dashboard and UE results below remain tied to their tested revisions;
no radio or GPU performance tests were repeated during the merge.

| Tested revision | Recorded checks | Evidence |
|---|---|---|
| Local merge `e59e095` | Python 90/90, both frontend regressions and shell syntax; new forwarding regression fails before the fix | [Merge report](main-merge-validation.md) |
| Channel fixes through `16af288` | CTest 12/12; Python 75/75; nine GPU stages; Compute Sanitizer memcheck/initcheck/synccheck with zero errors | [Merge-fix report](sionna-merge-fixes-validation.md) |
| Dashboard recovery `d6637f7` | Python 86/86; both frontend regressions; actual OCUDU heartbeat, independent interruption and recovery tests | [Metrics report](metrics-recovery-validation.md) |
| UE recovery `daa167ae3` | 18/18 focused tests; baseline defects reproduced before their fixes | UE report identified above; existing full NR RRC fixture failure remains outside the passing selection |

The nine GPU stages cover build, CTest, clean/AWGN relays, interference,
multi-gNB, TDL-A, correlated MIMO, and correlation control against a running
broker. These synthetic stages do not prove UE attachment. Conversely, an
attachment or a fresh dashboard row does not prove zero-miss processing.

The [paired ten-minute broker measurements](sionna-merge-fixes-validation.md#sustained-comparison-and-browser-validation)
failed the strict deadline criterion on both reviewed and fixed revisions.
Geometry-change resets, unsupported dynamic matrices on host staging,
intermittent moving traffic, incomplete UE mobility procedures and that
real-time failure remain explicit limits. No threshold relaxation, scheduling
change or new runtime qualification accompanies this documentation update.

## Native launch and historical design notes

For Docker-free 1×1 and rank-1 launch commands, remote dashboard access,
shutdown and troubleshooting, use the [native runtime guide](../scripts/native/README.md).
The native 1×1 launcher defaults to a requested 10 Hz Sionna update rate;
other launchers have their own defaults, and the achieved rate depends on
ray-tracing and control latency. The dashboard is a read-only observer:
RRC/PDU logs and restored traffic establish UE connectivity, not CUDA readiness.

The root contributor explanation, handover and milestone notes were retired
from the release checkout. Their original contents and authorship remain in
Git. Historical design and measurement context is collected in the
[rank-1 report supplement](rank1-feasibility-report.md#historical-design-and-measurement-context).

## Channel-generation profiling

`apps/sionna_bridge/run_bridge.py --profile-timing` adds
`timing_ms.generation_stages` to each channel-update record. Existing timing
fields retain their meaning. Each frequency/array group reports array setup,
`path_solver`, `cir_numpy`, remaining array export, optional geometry export,
tap/status packing, and path-polyline formatting in milliseconds. These are
host wall times, without extra GPU synchronization: lazy GPU work can finish
during a later materialization. They are not CUDA kernel timings.

The optional UI geometry accessor now runs inside its exception handler. A
component-build failure disables ray drawing and preserves channel generation;
it is not a claimed performance optimization.

For a repeatable diagnostic without launching gNB, UE, or broker processes:

```bash
python apps/sionna_bridge/profile_replay.py \
  --replay /path/to/sionna-status.jsonl \
  --scenario use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk.json \
  --out /path/to/profile.json --samples 40 --rounds 2 --pace-hz 10
```

This reuses recorded scene-frame positions and velocities, including the
already-applied arena offset. It writes a summary plus `profile-samples.jsonl`
with stage timings, profile coefficients, fingerprints and source timestamps.
Five first-position warmups are excluded; other positions may still incur
JIT costs. `--pace-hz 0` runs back to back. `--path-polylines 0` disables UI
geometry, and `--samples-per-src` changes the search budget explicitly. Inspect
recorded/effective environments and per-round results before comparing runs.
The measured generation interval excludes live runtime-status writes and does
not exercise control delivery, radio scheduling, or channel continuity.

On 2026-09-30, a DGX Spark GB10 (driver 580.178.04; Sionna RT 2.0.1,
Mitsuba 3.8.0, Dr.Jit 1.3.1, CUDA RT backend) replayed 40 positions from R4b
`20260929T144912Z` twice at 10 Hz, about eight seconds after warmup. Topology:
one gNB/two UEs, 1×1 antennas, four FDD links at 1842.5/1747.5 MHz,
23.04 MS/s tap conversion, depth 3, 200k samples/source, LOS/reflection/
refraction enabled, six UI polylines. No radio/broker or other GPU process
was running. Solver settings, carriers, sample rate and gain calibration
matched the recorded environment. Final replay mean/p50/p90/max generation
times were **23.0/21.9/22.9/79.5 ms** (80 measured updates). Path-solver calls
accounted for 20.65 ms/update on average; CIR export 0.52 ms and geometry export
1.36 ms. This is not a live 40 ms deadline pass.

An earlier paced pair measured 21.5 ms mean with geometry and 21.6 ms without;
there is no demonstrated mean-time improvement from disabling visualization.
The first unpaced run measured 37.3 ms mean, so cache, pacing and execution
conditions must be retained with the evidence. Profile hashes across separate
runs were not all bit-identical; they are diagnostics, not a numerical
equivalence claim. Historical R4b live generation averaged 87.8 ms, but that
run also executed the radio stack and broker. The historical/live difference
does not isolate a cause, and motion alone is not established as its cause.

Raw summaries and samples are under `/workspace/gpuch/sionna-profile-0930/`
on Spark, copied to `/home/minwoo/ocudu-work/sionna-profile-0930/`. The active
R7 source tree and launchers were not changed. Focused adapter, external-position
and Web UI tests passed 83/83 on Spark; a four-update bridge dry run exercised
the new JSON timing fields. Live profiling under radio load remains unmeasured.
