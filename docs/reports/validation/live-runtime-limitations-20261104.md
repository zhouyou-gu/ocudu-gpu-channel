# Live runtime limitations — 4 November 2026

**Sustained 1000 slots/s with zero starvation remains unqualified.** Successful attachment, packet delivery, low channel-processing latency and zero detected overflow are separate results. This record consolidates the November timing investigations; [compact measurements and source pins](live-runtime-limitations-20261104.json) accompany it.

## RTX 5090: delivery passes while radio time falls behind

On 3 November, source `c3135a9` ran one CPU-PHY OCUDU gNB and two srsUEs through the CUDA channel broker for 600 seconds. Hardware was Intel Core Ultra 9 285K / RTX 5090; radio settings were 20 MHz, 15 kHz and 23.04 MS/s, with a moving robot-ring scenario and Sionna updates at 10 Hz.

All **3,229,424 packets** arrived, but progress averaged **997.472 slots/s**, accumulated **1.517 seconds of lag**, and recorded **72 whole-process input starvations**. Overflow, sample-sequence gaps and ZMQ errors were zero. ZMQ can wait for IQ without reporting a hardware-style underflow, so clear fault counters do not establish correct radio speed.

Short experiments identified several waits:

- The sequential OCUDU PHY worker explicitly uses normal priority. One reproduced 1 µs sleep lasted 9.723 ms: its timer fired after approximately 52 µs, followed by 9.670 ms waiting for CPU. The original historical 10.512 ms call lacked a kernel trace.
- A separate synthetic probe directly captured an approximately 6 ms non-preemptible Intel display power-resume path. Matching that callback to earlier live kernel-worker stalls remains an inference.
- Raising PHY priority and separating display work produced inconsistent short results: 984.237→992.230 slots/s within one process, then 981.943 slots/s in a separate 30-second run with verified interrupt separation. These measurements do not qualify a production fix.
- With those settings active, a 30-second trace captured a remaining dependency: the normal-priority gNB `ZMQbg/IO/0` thread waited **4.155 ms runnable** behind background work. PHY receive waited **4.865 ms for IQ**, and the next scheduler entry followed after **5.166 ms**. Another capture showed the gNB's own radio poller competing with transport. PHY priority alone does not raise the threads delivering its samples.

Increasing fixed catch-up improved observed lag in one 600-second comparison, but worsened bulk-delay tails and lost packets; dynamic margins did not consistently help. Shared-host load and profiling overhead limit comparisons. No experimental pacing, priority, CPU-isolation or display setting was promoted to production; temporary settings were restored.

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

This is an evidence/status review, not a new radio qualification run. The latest committed profile-activation correction passed CPU/CUDA CTest 13/13 each and GPU checks 9/9 on RTX; see its [regression report](profile-activation-20261103.md). The broader October project checks remain dated evidence rather than fresh November test claims.
