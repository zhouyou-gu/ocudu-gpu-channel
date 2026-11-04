# Live runtime limitations — 4 November 2026

**Sustained 1000 slots/s with zero starvation remains unqualified.** Successful attachment, packet delivery, low channel-processing latency and zero detected overflow are separate results. This record consolidates the November timing investigations; [compact measurements and source pins](live-runtime-limitations-20261104.json) accompany it.

Ongoing work is tracked separately from this dated report: [RTX timing #11](../../issues/0011-rtx-slot-delays.md), [Spark periodic gaps #12](../../issues/0012-spark-periodic-slot-gaps.md), [Spark CPU-reference equality #13](../../issues/0013-spark-cpu-iq-equality.md) and [Spark UE scaling #18](../../issues/0018-spark-cpu-ue-scaling.md).

## RTX 5090: delivery passes while radio time falls behind

On 3 November, source `c3135a9` ran one CPU-PHY OCUDU gNB and two srsUEs through the CUDA channel broker for 600 seconds. Hardware was Intel Core Ultra 9 285K / RTX 5090; radio settings were 20 MHz, 15 kHz and 23.04 MS/s, with a moving robot-ring scenario and Sionna updates at 10 Hz.

All **3,229,424 packets** arrived, but progress averaged **997.472 slots/s**, accumulated **1.517 seconds of lag**, and recorded **72 whole-process input starvations**. Overflow, sample-sequence gaps and ZMQ errors were zero. ZMQ can wait for IQ without reporting a hardware-style underflow, so clear fault counters do not establish correct radio speed.

Short experiments identified several waits:

- The sequential OCUDU PHY worker explicitly uses normal priority. One reproduced 1 µs sleep lasted 9.723 ms: its timer fired after approximately 52 µs, followed by 9.670 ms waiting for CPU. The original historical 10.512 ms call lacked a kernel trace.
- A separate synthetic probe directly captured an approximately 6 ms non-preemptible Intel display power-resume path. Matching that callback to earlier live kernel-worker stalls remains an inference.
- Raising PHY priority and separating display work produced inconsistent short results: 984.237→992.230 slots/s within one process, then 981.943 slots/s in a separate 30-second run with verified interrupt separation. These measurements do not qualify a production fix.
- With those settings active, a 30-second trace captured a remaining dependency: the normal-priority gNB `ZMQbg/IO/0` thread waited **4.155 ms runnable** behind background work. PHY receive waited **4.865 ms for IQ**, and the next scheduler entry followed after **5.166 ms**. Another capture showed the gNB's own radio poller competing with transport. PHY priority alone does not raise the threads delivering its samples.

Increasing fixed catch-up improved observed lag in one 600-second comparison, but worsened bulk-delay tails and lost packets; dynamic margins did not consistently help. Shared-host load and profiling overhead limit comparisons. No experimental pacing, priority, CPU-isolation or display setting was promoted to production; temporary settings were restored.

## Thirty-second ZMQ I/O priority follow-up

On 4 November, the same RTX runtime binaries completed one 30-second mixed-traffic test with gNB ZMQ I/O at **FIFO priority 20 on CPU 7**, PHY at FIFO 10, and all other gNB threads on CPUs 1–6. UE masks stayed 8–19 and the broker stayed on CPU 20. Display/workqueue separation remained active. Settings were applied before traffic and checked three times. CPU 7 was dedicated within the radio test; ordinary host processes retained their affinity and could run while I/O slept.

| Clock | Measured slots/s |
|---|---:|
| gNB, full 30-second window | 992.313 |
| gNB, common 28-second interior | 993.450 |
| UE0, common interior | 993.501 |
| UE1, common interior | 993.503 |

All **160,963 packets** arrived, with zero detected overflow, sequence gaps or ZMQ errors; the native connectivity/traffic gate passed without a hang. However, 38 whole-process starvations remained, including 1/4/3 on the gNB/UE0/UE1 sides in the heartbeat interior. Five gaps between consecutive logged slot decisions exceeded 5 ms, with a maximum 6.872 ms. Strict real-time qualification remains failed.

The bounded I/O scheduling trace retained 94,551 matched wake/run pairs: p99 was approximately 2 µs and the maximum observed wait was 187 µs. Earlier events were overwritten by the bounded buffers, so these are **retained-pair statistics**, not an all-30-second maximum or percentile. The measured I/O delays are shorter than the earlier captured 4.155 ms wait; the whole-radio rate is closer to target than the previous 981.943 slots/s full-isolation run, but roughly matches the earlier 992.230 slots/s short control. Shared-host activity, different CPU allocation and trace scope prevent treating this as a matched proof of throughput improvement.

Temporary thread policies, affinities, IRQ settings and the workqueue mask were restored; both diagnostic containers were stopped and the other 14 running containers preserved. Cleanup freed 1.02 GB of bulk data. The 2.07 MB scheduling trace, helpers and full summaries remain in ignored validation storage; only compact measurements were added to this record. No production default was changed.

## Source audit: what causes lasting lag

**The persistent deficit follows from the broker's clock policy; stalls can originate on either side of the IQ exchange.** This is an integration timing limitation, with configuration contributors, rather than evidence of an incorrect sample rate or a single slow channel kernel. The inspected runtime source is `c3135a9`, with OCUDU `a1916ed` and srsUE `eea87b1` as pinned in the JSON.

| Component | Source finding | Consequence |
|---|---|---|
| Broker clock | `pacing.h:49–62` rebases the deadline to `now - max_debt`; `broker.cpp:1145` sets that bound to one batch, 1 ms here. The first successful charge anchors the schedule. | Delays beyond the bound lose catch-up credit permanently; this is repeated rebasing, rather than a fixed future start time. IQ is retained. |
| Broker readiness | `broker.cpp:1210–1264` chooses the common available input window and output room, permitting partial batches; pacing follows computation at `1370–1383`. | Matching IQ can already start computation when output room exists. One late UE input can hold the summed uplink; IQ arrival alone does not guarantee publication. |
| Broker buffering | `broker.cpp:746–750` allocates `rx_ring_batches` capacity but fixes `rx_high_water` to one batch. | Increasing ring capacity alone does not increase usable producer look-ahead. |
| Broker handoffs | `broker.cpp:183–240` defaults to 50 µs sleep polling between in-process rings. The RTX launcher pins the broker process, including its workers and ZMQ I/O, to efficiency CPU 20. | Every handoff remains sensitive to timer wake-up and CPU contention. Raising only gNB I/O priority leaves these and UE transport threads unchanged. |
| OCUDU adapter | The ZMQ driver forces sequential lower-PHY execution (`ru_sdr_config_translator.cpp:253–256`). Its PHY waits in `radio_zmq_rx_channel::receive()` until requested samples exist; RX sample time advances only after receipt (`radio_zmq_rx_stream.cpp:82–110`). | A late transport wake freezes the software radio clock instead of producing an immediate hardware-style underflow. |
| OCUDU buffer handling | A full RX circular buffer retries inside `receive_response()` on the shared radio worker; RX ports are drained sequentially. | Unlimited catch-up can block sibling ports on multi-port radios. That explains the protective bound, but does not make wall-clock slowdown acceptable. This run had one port per radio; no such deadlock was observed. |

An RTX probe compiled the actual pacing header with a synthetic clock, without another live run. After ten timely 1 ms batches, the next batch was due at 10 ms but became ready at 14 ms. Its rebased deadline was 13 ms; after the permitted catch-up, subsequent deadlines remained **3 ms behind** the original schedule. Existing [October producer traces](../experiments/scheduler-pacing-trace-20261013/README.md) independently reconstructed this mechanism: RR lost 132.243 ms over 120 seconds while discarded pacing debt was 131.488 ms, with endpoint phase accounting for the difference. Those traces mostly located waits at IQ handoffs, and also contained rare channel-call tails over 1 ms. They do not attribute every remaining November gap to a particular thread.

The active configurations already set srsUE `rx_sleep=false`; OCUDU's separate `lphy_dl_throttling` is absent and defaults to zero. Sample rates agree at 23.04 MS/s. Container environment and launcher inspection found no broker polling/spin overrides, leaving the default sleep-poll policy. A separate propagation gap exists: `platform-profile.py:133` exports the `OCUDU_NATIVE_BROKER_ENV` string, but the scheduler inner launch at `356–360` never applies it. That can silently omit a requested profile's broker settings; it does not explain this RTX test, whose platform profile was `none`.

The source audit led to separating **retaining elapsed-time debt** from **limiting recovery rate and downstream occupancy**. The later clock-recovery change below implements that separation in the broker; the external adapter remains unchanged. Simply removing pacing or increasing an unrestricted burst repeats the known deadlock risk. The intervening handoff-only repair below retained the old clock policy.

## Broker handoff repair and short controls

The working-tree repair replaces default in-process sleep polling with targeted condition-variable notifications. A generation ticket is captured before checking ring state so a publication between the check and the wait cannot be lost. Each input publication, cursor advance, output publication and output release wakes the dependent worker; shutdown wakes sleepers. Explicit diagnostic polling/spinning still works. The scheduler launcher now applies `OCUDU_NATIVE_BROKER_ENV` and records it. Starvation accounting also checks elapsed wait **before accepting ready input**, preventing a late wake from hiding a missed deadline.

RTX validation passed **13/13 CTest cases** in a CUDA-enabled build, including four-port bit-exact comparison of notified, spinning and polling handoffs. Scheduler helper checks passed 28 tests with two skips. The profile regression rejected the original launch block; a separate transient-input regression rejected a mutation restoring the old starvation-check order. The final source and binary hashes are in the compact JSON. Neither channel arithmetic nor the one-batch pacing policy changed.

Four completed controls each used 30 seconds of the same mixed traffic and the prior radio workload. gNB I/O remained FIFO 20 on CPU 7, PHY FIFO 10, with display/workqueue separation. Both sides of the comparison used the same unarmed observer wrapper; no scheduling traces were recorded.

| Broker / allocation | gNB full window | gNB interior | UE0 interior | UE1 interior |
|---|---:|---:|---:|---:|
| Original polling, broker CPU 20 | 999.139 | 999.417 | 999.368 | 999.372 |
| Notified, broker CPU 20 | 998.947 | 999.487 | 999.512 | 999.486 |
| Notified, broker CPU 6; gNB moved off 6 | 992.668 | 992.531 | 992.451 | 992.496 |
| Final repair with corrected counters, broker CPU 20 | 993.628 | 993.694 | 993.661 | 993.647 |

Rates are slots/s; interiors exclude one second at each endpoint. The CPU 6 trial preserved five gNB main workers by changing gNB affinity after startup; it reduced gNB CPU eligibility from 1–6 to 1–5. It was worse and was not retained. Broker CPU 6 was requested through the launcher's `taskset`; a live per-thread mask snapshot was not retained. Ordinary host workloads remained eligible for these CPUs. One UE1 log record was out of order in that trial; the clock uses net unwrapped endpoint progress.

Every completed run delivered all **160,963 packets**, with no detected queue overflow, sample-sequence gaps, ZMQ errors or a hang. The final build still lost **191.160 ms** of radio progress over the full window, had a **6.861 ms** largest consecutive logged slot gap, and counted **22 whole-process starvations**, with 1/3/2 in the gNB/UE0/UE1 heartbeat interior. Earlier controls predate the resumed-wait counter correction and can undercount starvation; their clock and delivery measurements remain separate evidence. Two setup-only attempts stopped before measurement without applying scheduling changes and are excluded from the table.

**This repair does not establish a timing improvement or solve strict real time.** The small difference in the first comparison is below the variation between runs, and the final repeat was slower. Current measurements cannot assign every remaining delay to one thread. They support retaining the functional notification/configuration/accounting repairs, while rejecting CPU 6 as a demonstrated remedy.

The next architectural option identified at this stage was a repository-carried OCUDU adapter patch for fair, nonblocking buffer service, paired with recovery that retains clock phase while bounding rate and downstream occupancy. Replacing local TCP endpoints with IPC is a smaller, untested transport experiment; a shared-memory transport requires adapters on both stack sides. Neither transport substitution alone changes the clock policy or eliminates OS/GPU scheduling delays. This handoff-only revision changed neither the external stacks nor recovery policy; the subsequent broker-only clock change follows.

Thread/IRQ/workqueue settings were restored and both diagnostic containers stopped; 14 other running containers were preserved. Cleanup pruned **4.16 GB** of bulk logs plus unarmed observer buffers. Helpers, remaining evidence and raw traces stay ignored; only compact findings are added to Git.

## Broker clock recovery: retained time with bounded releases

The subsequent broker-only change keeps the **nominal deadline fixed to its original schedule**, while a separate limiter caps immediate release credit at one batch and sustained recovery at **1% above nominal**. Only unused limiter credit expires; nominal elapsed time is never discarded. Actual publication is charged after a late wake, and fractional sample durations carry between fragments. `runtime.pacing_recovery_ppm` defaults to 10000 and accepts 0–10000; zero disables sustained acceleration. Output headroom remains one batch. Neither OCUDU nor srsUE was changed.

The CUDA-enabled build passed **13/13 CTest cases**. Synthetic-clock tests verify recovery from a 4 ms delay, eventual recovery after a one-second stall with no unrestricted burst, accounting for a late sleeping publication, fragmented-time accuracy and 24-hour duration arithmetic. Two targeted mutations—rebasing the nominal deadline and omitting actual-publication accounting—were rejected by the regressions.

Run `20261104T032414Z` then completed **one 30-second test** with the same RTX 5090 / Core Ultra 9 285K, one CPU-PHY gNB, two srsUEs, CUDA broker, moving seed-1 robot-ring scenario, 20 MHz / 15 kHz / 23.04 MS/s and mixed traffic. Temporary CPU/priority settings matched the preceding CPU 20 controls, with no raw scheduling trace.

| Clock | Slots/s |
|---|---:|
| gNB, full 30-second window | **999.374** |
| gNB, common interior | 999.485 |
| UE0, common interior | 999.534 |
| UE1, common interior | 999.503 |

All **160,963 packets** arrived. Detected queue overflows, sequence gaps, ZMQ errors and gNB logged RF/queue failures were zero; no hang occurred. Every broker output-ring peak remained **23040 samples, or 1 ms**. Full-window gNB drift was **18.766 ms**, compared with 191.160 ms in the preceding final handoff build. The earlier original-polling control already reached 999.139 slots/s; shared-host load varied and this is not a paired statistical proof of a repeatable gain.

Catch-up was independently visible in the gNB scheduler clock: from measurement offset 18.488 to 28.485 seconds, **10049 slots over 9.996606 seconds** gave **1005.241 slots/s** and recovered **52.394 ms**. The broker's corresponding retained-delay gauge fell by 52.461 ms. This is a recovery interval, not a whole-run speed claim, and it stays within the 1% allowance.

**Strict timing remains failed:** 42 whole-process input starvations were counted, including 0/1/2 in the gNB/UE0/UE1 heartbeat interior; the largest consecutive logged slot gap was **8.652 ms**. Retained delay did not clear overall. Gauges are relative to each node's first publication and include startup waits: gNB interior delay went from 72.723 to 79.194 ms, while the UE gauges include approximately 3.3 seconds of startup offset. They must not be added together or confused with the 30-second window's incremental drift. The nominal clock was not reset to remove these values.

The short result supports the broker recovery mechanism and observed single-port buffer safety, not arbitrary multi-port or long-run qualification. IRQ/workqueue/thread settings were restored; the two diagnostic containers were stopped and 14 other containers preserved. Another **1.01 GB** of bulk logs and the unarmed observer buffer were pruned; helpers, remaining logs and raw traces stay ignored.

## Radio and UE transport separation

Two untraced 30-second controls kept the clock-recovery broker, native stack binaries, workload and one-batch output bound unchanged. gNB radio moved to CPU 6; gNB ZMQ I/O used CPU 7 and UE0/UE1 ZMQ I/O used CPUs 8/9, all I/O at FIFO priority 20. PHY stayed FIFO 10 on CPUs 1–5, other UE threads used CPUs 10–19, and the broker stayed on CPU 20. Python threads were excluded from CPUs 6–9, including periodic checks for newly created threads. Three snapshots verified placement and policy during each run.

| Run | gNB full window, slots/s | UE0 / UE1 interior, slots/s | Slot-gap p99 / max, ms | Gaps above 5 ms |
|---|---:|---:|---:|---:|
| Earlier clock-recovery control | 999.374 | 999.534 / 999.503 | 2.569 / 8.652 | 2 |
| Isolated radio at FIFO 15 | 974.785 | 974.915 / 974.837 | 1.932 / 34.642 | 34 |
| Isolated radio at normal scheduling, nice −10 | **1000.320** | **1000.283 / 1000.258** | 2.367 / 8.352 | 9 |

Gap statistics use consecutive logged slot numbers. The FIFO radio worker busy-polls, and the first control introduced recurring 33–35 ms pauses near one-second boundaries. The host's real-time CPU budget was 950000 µs per 1000000 µs, unchanged throughout. The periodic pattern and its disappearance with normal scheduling support **real-time CPU-budget throttling** as the explanation, but no direct throttle event was captured. Linux documents this budget separately from thread priority in its [real-time scheduling description](https://www.kernel.org/doc/html/v6.8/scheduler/sched-rt-group.html).

The corrected control recovered 9.590 ms over its measurement window; slightly exceeding 1000 slots/s is permitted by the unchanged 1% recovery ceiling. This is an average-rate observation, not strict timing qualification or a matched proof of general improvement: background load varied, nine gaps still exceeded 5 ms, and whole-process starvations increased from 42 to 55. The heartbeat interior counted 6/4/5 starvations at gNB/UE0/UE1. Of 160963 scheduled packets, **160961 arrived**, with one missing UE1 downlink control packet and one missing video packet. No broker overflow, IQ sequence gap, ZMQ error, logged RF failure or hang was detected; output peaks stayed 23040 samples per port. The native connectivity/traffic gate passed, which does not require perfect packet delivery or strict timing.

## Remaining delay causes after transport separation

A further bounded 30-second diagnostic used the corrected placement above and captured two long gaps. It excluded busy-polling gNB radio and PHY threads from syscall tracing, while recording their radio-call boundaries, all CPU switches, targeted transport waits, UE RF calls and broker CUDA/ring calls. Two earlier captures without this placement had already shown a 3.773 ms gNB radio dispatch wait behind Python and roughly 3 ms UE transport dispatch waits; those were distinct observed incidents.

The new captures identify two remaining components and mechanisms:

| Captured slot gap | First stalled dependency | Propagation to the slot |
|---|---|---|
| **7.315 ms** | gNB ZMQ I/O, TID 3468012, was ready at FIFO 20 on CPU 7 but waited **6.215 ms** while `kworker/7:1` ran the display callback `output_poll_execute`. | The broker had sent the IQ reply at monotonic 494173779123374 ns. I/O became ready at 494173779161000 ns, ran at 494173785376000 ns and completed the payload read at 494173785389000 ns; PHY receive returned at 494173785438000 ns, then the next slot entered at 494173785629000 ns. |
| **6.828 ms** | UE0 `SYNC`, TID 3468229, was ready on CPU 19 for **5.934 ms**, behind Python TID 3385151 in the background physical-AI `ocudu_physical_ai_runtime.metatron_worker` process. | The receive-buffer wait woke at 494182600481000 ns but SYNC ran only at 494182606415000 ns and returned from RF receive at 494182606435000 ns. UE0 uplink IQ reached the broker at 494182607368738 ns; CUDA began approximately 18 µs later, finished in 0.548 ms, and PHY receive returned at 494182608096000 ns before the next slot at 494182608258000 ns. |

In the first capture, the display callback entered at 494173754005000 ns and remained active through the I/O delay; CPU 7 had only 6 µs of captured IRQ/softIRQ overlap during the slot gap. I/O had used only 54.5 ms of FIFO CPU time in the preceding second, far below the 950 ms budget. This is a kernel dispatch delay during display work, not the FIFO busy-poll throttling introduced in the rejected control. The exact inner display-driver instruction was not captured by a kernel stack.

**The display separation was incomplete.** `output_poll_execute` reschedules through `schedule_delayed_work()` on the CPU-bound system workqueue. Changing the unbound workqueue mask and display IRQ affinities does not move that bound callback. This distinction is visible in the [Linux 6.8 display polling source](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/drm_probe_helper.c), the [delayed-work API](https://github.com/torvalds/linux/blob/v6.8/include/linux/workqueue.h), and [system workqueue initialization](https://github.com/torvalds/linux/blob/v6.8/kernel/workqueue.c). The callback still ran on the protected I/O CPU in the trace.

**The UE separation covered I/O, not the full sample-producing path.** SYNC remained a normal-priority thread on CPUs 10–19, where the physical-AI Python workers remained eligible. It was delayed after its receive-buffer wake, rather than waiting those 5.934 ms for IQ to arrive. This delayed UE progress and subsequent uplink availability; the broker could not compute the required summed uplink before the late input existed. The pinned srsUE path is `rf_zmq_recv_with_time_multi()` → `rf_zmq_rx_baseband()` in `lib/src/phy/rf/`, called by SYNC.

MAC scheduling computation took **10 / 18 µs** in the two captured gaps. The longest overlapping CUDA calls were **0.167 / 0.548 ms**. These captures therefore locate the dominant delays in display kernel work and UE CPU dispatch, rather than slow MAC computation or a long channel kernel. They establish two incidents, not the cause of every historical miss or the exact earlier untraced 8.352 ms gap.

The diagnostic passed native connectivity/traffic checks, delivered all 160963 packets and recorded zero overflow/IQ gaps/ZMQ errors, with 26 whole-process starvations remaining. Its gNB full-window progress was 999.855 slots/s; tracing changes overhead, so this is not an untraced performance control. Strict real-time qualification remains failed.

All temporary thread policies, nice values, Python affinities, IRQ affinities and workqueue masks were restored. Independent checks verified 139 surviving changed Python threads; the remaining changed thread had exited. Owned probes and trace instances were removed, both diagnostic containers stopped, and 14 unrelated containers preserved. Cleanup across these investigations freed **4.22 GB**; **2.23 MB** of selected traces remain outside Git, including the display callback entry. Full radio logs, bulk traces, hop CSVs and observer buffers were hash-recorded before removal. No production scheduling default or external stack source changed.

## Spark GB10: short CPU separation and project review

The current clock-recovery source was also built for **aarch64 / CUDA SM 121** on DGX Spark (20-core GB10, Linux 7.0.0-1019-nvidia, CUDA 13.0, driver 580.178.04). The workload used one native CPU-PHY OCUDU gNB and two single-port srsUEs, the CUDA broker, live Sionna at 10 Hz, moving robot-ring scenario seed 1, mixed traffic, 20 MHz / 15 kHz / 23.04 MS/s. C++ source hashes match the reviewed RTX source; ARM and x86 binaries differ. The existing pinned Spark stack and its recorded NEON LDPC patch were retained.

Background applications were restricted to CPUs 0–4; radio work used CPUs 5–19. PHY used CPU 5 at FIFO 10, gNB radio CPU 8 at normal scheduling/nice −10, gNB I/O CPU 12 at FIFO 20, UE I/O CPUs 13/14 at FIFO 20, and UE SYNC CPUs 9/17 at FIFO 10. Other UE work used CPUs 15–16/18–19; broker I/O used CPU 11 at FIFO 20 and other broker work CPU 10. Three snapshots and 0.5-second affinity enforcement found no background application eligible on radio CPUs. This excludes application CPU competition, not kernel work, interrupts or shared memory/GPU effects; live Sionna is itself part of the tested workload.

One setup attempt was excluded because placement took 3.187 seconds and exceeded the original 3-second traffic lead. The diagnostic copy's lead was extended to 12 seconds, and placement completed before both retained measurements. No canonical launcher timing or runtime scheduling default changed.

| 30-second run | gNB full / interior slots/s | UE0 / UE1 interior slots/s | Maximum consecutive slot gap | Packets received / scheduled |
|---|---:|---:|---:|---:|
| Scheduler/IRQ diagnostic | 999.917 / 1000.119 | 1000.128 / 1000.071 | 3.704 ms | 160963 / 160963 |
| Reduced scheduler audit | **999.979 / 1000.126** | **1000.115 / 1000.135** | **9.749 ms** | **160962 / 160963** |

Both native connectivity/traffic gates passed, with zero detected broker overflow, IQ sequence gaps, ZMQ errors or logged RF failure. The first run had two whole-process input starvations, appearing during early startup. The second had 14 whole-process starvations and one missing UE1 downlink control packet; its full-window incremental drift was 0.640 ms. Close-to-1000 average progress does not remove the remaining timing failures.

**The unresolved Spark issue is periodic slot delay despite application CPU separation.** The reduced-audit run recorded four gaps above 5 ms: 9.749, 9.373, 9.203 and 8.872 ms at measurement offsets 4.496, 9.496, 14.496 and 24.497 seconds. The roughly five-second pattern is observed; its cause was not established before experiments were stopped. It must not be attributed to the RTX display/Python mechanisms without a Spark causal capture.

The first trace hit its 16 MiB cap and retained only 8.693 seconds of measured traffic, with many overwritten events. The reduced audit retained the full measurement in time (0.740 MB), but lost **120 CPU-5 events**. Retained switches showed no competing application CPU time, and host identities resolved the remaining observed tasks as kernel threads; kernel tasks, including NVIDIA modeset work, still ran on radio CPUs. These observations do **not** prove which wait or kernel operation caused the periodic gaps. The partial/lossy traces do not certify an entirely interruption-free host or strict real-time operation.

The pending broker changes were reviewed for retained nominal time, bounded release/recovery, notification ordering, lock ordering, shutdown wakeups and launcher environment propagation. No additional runtime-code defect was identified in that review. Focused regression results are:

| Platform | CUDA-enabled CTest | Scheduler helper tests | Limitation |
|---|---|---|---|
| RTX 5090 | **13/13 passed** | 28 passed, 2 skipped | Live strict timing remains failed |
| Spark GB10 | **12/13 passed** | 28 passed, 2 skipped | `cpu_parallel` fails serial/parallel IQ bit equality in the fading + LOS case |

The Spark CPU-reference failure also reproduced with its older broker build; `cpu_backend.cpp`, `sample_executor.cpp` and `test_cpu_parallel.cpp` have identical hashes in the old and reviewed source trees. This is an existing unresolved CPU-reference limitation, not evidence that the timing changes caused it. It was neither hidden nor fixed by weakening the check. The CUDA live path passed the stated delivery/runtime checks, but the project cannot claim an all-green Spark suite or strict timing on either platform.

At the stop request, no test gNB, UE or broker remained active. The helper container and owned trace instance were removed, original application affinities/priorities and requested IRQ/workqueue settings restored, and four unrelated Spark containers preserved. Effective IRQ routing can be reselected by the kernel after restoring requested masks. Cleanup freed **2.30 GB** of new Spark bulk data; selected traces total **0.762 MB** and remain ignored. Builds, compact summaries and hashes remain available outside Git. The older Spark demo and its local tunnel were also confirmed stopped.

## Spark GB10: CPU radio capacity is also limited

The separate 4 November CPU-PHY campaign used one OCUDU gNB, srsUEs and the CUDA broker with precomputed moving SUTD replay, 20 MHz / 15 kHz / 23.04 MS/s. It used archived runner revision `364391b`, not the RTX experiment's source/workload. Two cold runs per point were audited against the gNB and every UE's independent clocks.

| UEs | Original allocation, slots/s across radios | More UE CPU access | Strict real time |
|---:|---:|---:|---|
| 1 | 996–999 | Not tested | Failed |
| 2 | 997–998 | Not tested | Failed |
| 4 | 997 | Not tested | Failed |
| 8 | 714–727 | 855–858 | Failed |
| 16 | 300–323 | 455–469 | Failed |

Expanding only UE CPU eligibility with identical recorded inputs improved the overloaded runs. This establishes UE CPU availability as a contributor, alongside radio transport and scheduling; it does not isolate all delay to PHY computation or prove that GPU PHY offload alone restores real time. Counts 3 and 5–7 were not measured, and four UEs are not a strict real-time capacity pass.

These CPU-PHY results do not replace the separate, revision-specific [contributor CUDA-gNB measurements](../performance/2026-10-27-gb10-cuda-gnb-live/README.md). Synthetic channel-only latency is also a different measurement from full live-radio capacity.

## Current boundary and evidence retention

Continuous moving **single-cell/two-UE packet delivery** passed the stated RTX run. Continuous moving **two-cell/two-UE operation** retains its earlier failure; the single-cell result does not qualify it. Strict zero-miss timing remains failed across these investigations. Multi-port live DL activity, hardware timing, geometry continuity and handover retain the limits on the [status page](../../getting_started/status.md).

The five detailed experiment bundles and temporary helpers were archived and hash-verified outside tracked source before cleanup. Raw logs and captures are excluded from new Git additions; the compact JSON records the archive digest, source revisions and selected causal timestamps. The validation archive is `validation/runtime-cleanup-20261104/`; the separate Spark campaign retains its existing evidence. Some earlier bulk radio logs were already pruned, so analyses requiring those raw files need a rerun.

The short controls above do not qualify strict real time. The earlier committed profile-activation correction passed CPU/CUDA CTest 13/13 each and GPU checks 9/9 on RTX; see its [regression report](profile-activation-20261103.md). The broader October project checks remain dated evidence rather than fresh November test claims.
