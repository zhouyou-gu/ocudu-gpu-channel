# gNB metrics recovery validation

> Dated evidence, preserved from `docs/metrics-recovery-validation.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../getting_started/status.md).


This is a dated validation record. See the [current integration guide](../../history/milestones/sionna-integration-20260914.md) for subsequent channel, dashboard and UE recovery changes; the results below remain tied to their recorded revisions.

The dashboard fix is commit `d6637f7` on `integration/sionna-history-fix`, following reviewed commit `5c6cafd`. Contributor tip `066a702` remains an ancestor. Changes are confined to metrics transport, RAN status rendering and tests. OCUDU, srsUE, Sionna, CUDA and tracked launcher scripts are unchanged by this fix.

## Confirmed defects and correction

The previous WebSocket reader treated five seconds of silence as a socket failure. The metrics worker closed that connection and resubscribed. This reader was already present in the contributor branch; it was not introduced by the merge. A second defect in the multi-gNB implementation allowed an acknowledgment after reconnection to make recently cached scheduler rows appear fresh.

The correction sends a Ping after five seconds without a completed incoming frame and requires a matching Pong within ten seconds. Interruptible reads retain partially decoded frames and fragmented messages. Scheduler freshness remains five seconds and is independent of heartbeats. `ue_list_current_connection` prevents historical rows from becoming current until a scheduler report arrives on the new connection.

Both defects were reproduced on the RTX 5090 against `5c6cafd` before deployment. The deterministic idle reproduction shortens the socket timeout to 50 ms without changing its error path; the live baseline observes the original five-second setting. Evidence is `recovery-before-regressions.log` and `recovery-baseline.jsonl` under the remote artifact root.

## Environment and commands

All tests ran on the RTX 5090 host. The remote artifact root is `$HOME/ocudu-gpu-channel-workspace/validation/sionna-history-20260913/fix-validation` (`v` below); `w` is `$HOME/ocudu-gpu-channel-workspace`.

```bash
cd "$v/project"
"$w/venvs/sionna/bin/python" -m unittest discover -s tests -p 'test_*.py'
docker run --rm --network none -v "$v/project:/work:ro" -w /work node:22 node tests/test_web_ui_ran.mjs
docker run --rm --network none -v "$v/project:/work:ro" -w /work node:22 node tests/test_web_ui_disconnect.mjs
```

Result: **86 Python tests passed**, including seven transport/session recovery tests; both frontend regressions passed. Tests cover idle heartbeat survival, unmatched/missing Pong rejection, partial headers/payloads, fragmentation, server Ping, invalid control frames, closure, interruptible shutdown, reconnect acknowledgments, empty lists, returning RNTIs, independent feeds and legacy rendering. Git whitespace checks passed.

The live session is `20260914T034758Z`: two four-port OCUDU gNBs, two one-port srsUEs, Open5GS, the CUDA broker and the contributor's original moving `multi-gnb-sutd.json`. Requested Sionna updates and broker telemetry remain 500 Hz; sample rate remains 23.04 MHz. The scenario SHA-256 is `9a67cd16d9aa7d06078598f7f5f48e74df0181bfdc0025448d8a241b02daf01c`.

OCUDU source checkout: `2563975a74baf94db51b4a41d6ff48e945cbb8f7`. Running srsUE source: `7bbd44316940f875fb351aa7d031672f8c37d904`. Image hashes, container IDs/start times and deployed file hashes are in `recovery-runtime-manifest.txt`. The adapted test launcher and inherited srsUE fork differ from the contributor launcher's upstream `release_23_11` default; this is not claimed as an exact reconstruction of the contributor's demonstration.

## Live validation

The five-minute baseline (299.368 seconds between first and last samples) recorded **13 sampled disconnected episodes per gNB**, with maximum scheduler receipt gaps of 10.633/10.639 seconds. Only the Web service was then replaced. Temporary transparent proxies connect the Web service to the real metrics endpoints and record Ping/Pong and transport events. A separate capability probe confirmed the live OCUDU endpoint answers matching WebSocket Pongs (seven Pings, seven Pongs, using a shortened test-only idle interval).

The ten-minute validation completed in 603.092 seconds including final traffic checks, with 488 snapshots spanning 598.901 seconds. It recorded browser state without reloading the page, API data, radio processing progress and UE RRC/PDU/release counters. gNB0's metrics path was interrupted at 60.404 seconds and restored at 69.095; gNB1 was interrupted at 120.358 and restored at 129.187. The approximately eight-second planned interruptions affected only metrics TCP connections.

| Check | Result |
| --- | --- |
| Actual OCUDU heartbeats | Each gNB answered **83/83** Pings during the observation. |
| Unplanned metrics reconnects | **Zero**. Each feed had exactly one closure/reconnection caused by its deliberate interruption. |
| Independent recovery | gNB0 reconnected at 76.354 s with historical rows, then received a scheduler report at 77.674 s. gNB1 reconnected at 135.564 s with historical data, then received a scheduler report at 136.767 s. The other feed stayed connected during each interruption. |
| Browser recovery | Both groups displayed the waiting-for-report state; one page marker persisted throughout; **zero captured JavaScript exceptions**. |
| Existing controls | Rays and labels toggled and restored, reset-view ran, and antenna selection was verified on the replacement DOM element after rendering. |
| Channel processing | All ten links advanced; sequences increased by 6,730. Per-link processing calls advanced 198,957–322,524, and corresponding nominal sample-slot counters advanced 166,192–166,193. These are progress measurements, not a real-time qualification. |
| UE traffic | Both UEs: **0/10 replies at the start and 0/10 at the end** to `10.45.1.1`, through their actual UE network namespaces. |
| Automatic UE reassociation | **Failed in this observation**: each UE retained one initial RRC/PDU, one printed scheduling-request failure/release message and eight random-access attempts; no new successful RRC/PDU appeared. The real returning-UE display path consequently remains unverified; controlled return/identity regressions passed. |

Both scheduler feeds continued to have stale intervals; the fix preserves their warnings. Fresh scheduler reports sometimes retain the gNB's old zero-throughput UE context, which is not proof that the UE is connected. Source inspection of OCUDU `lib/mac/mac_ctrl/mac_metrics_aggregator.cpp` shows report boundaries depend on radio-slot progression. That explains why the configured report period is not a guaranteed wall-clock delivery interval, but this patch does not establish the underlying cause of slow runtime progression.

After validation, direct subscriptions to loopback 18001/18002 were restored and the temporary proxies were stopped. The five original radio/core containers retained their IDs and start times throughout. The single Web service remains on port 19080; the original supervisor inspection window ends around 12:49 Singapore time on 2026-09-14.

Live commands and artifacts: `observe-recovery.py`, `recovery-proxy.py`, `recovery-live-check.py`, `recovery-live.jsonl`, `recovery-proxy.jsonl`, `recovery-browser-*.png`, `recovery-*-ue*.log`, and `recovery-ue-lifecycle.json`.

## Qualification boundary

The dashboard cannot cause UE reassociation. A returning scheduler row or reused RNTI does not prove a new RRC/PDU session. Automatic reassociation is assessed from UE logs and restored traffic, separately from WebSocket recovery. No UE restart is permitted to manufacture a successful reassociation result. No GPU performance qualification is repeated or implied.

## Follow-up: source-level recovery diagnosis

Inspection of the running srsUE source at `7bbd443` found that the console message `Scheduling request failed: releasing RRC connection...` does **not** prove RRC release. `srsue/src/stack/mac_nr/proc_sr_nr.cc` calls `rrc->release_pucch_srs()`, which is empty in `rrc_nr.cc`, clears pending grants and starts random access. When preamble attempts are exhausted, `proc_ra_nr.cc` calls `mac.rrc_ra_problem()`; that forwards to `rrc_nr::ra_problem()`, whose standalone-NR branch is a TODO. The recovery callback therefore has no standalone recovery action.

The same source has empty `in_sync()`, `out_of_sync()`, `max_retx_attempted()` and `protocol_failure()` callbacks. Its `rrc_release()` resets several lower layers but does not itself set the RRC state to IDLE or restart NAS/cell selection. `setup_request_proc::init()` refuses a new connection request unless the state is IDLE. These are concrete incomplete recovery paths and a strong explanation for the observed terminal recovery state. The trace still needs explicit callback/state instrumentation to establish which terminal branch executed during this run.

The artifact counter named `release` counts that console string only; it is not a verified RRC-state transition. Previous prose describing a confirmed RRC release was too strong. Fixing this requires a separate, coordinated UE RRC/MAC/NAS recovery change; restarting the UE process is not evidence of automatic reassociation. The cause of the initial SR/RA failures and channel/timing behavior remain separate questions.
