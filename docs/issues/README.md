# Known issues

[GitHub Issues](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues) owns the live status, assignments and discussion. This folder keeps one concise technical record per issue, named `NNNN-short-name.md`; each record links to its GitHub issue. Status below is a snapshot from **4 November 2026**.

Track confirmed defects, regressions, measured limitations and failed qualification. Search existing open and closed issues before adding a record; update a matching issue rather than duplicating it. Planned or unimplemented features remain in the [design inventory](../development/designs/README.md) unless explicitly tracked as issues.

## Recorded open issues

| GitHub | Local record | Category |
|---|---|---|
| [#11](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/11) | [RTX: strict slot timing still fails after broker clock recovery](0011-rtx-slot-delays.md) | Measured timing failure |
| [#12](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/12) | [Spark: periodic 8.9–9.7 ms slot gaps remain with separated application CPUs](0012-spark-periodic-slot-gaps.md) | Measured timing failure; cause unresolved |
| [#13](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/13) | [Spark: cpu_parallel fails serial/parallel IQ equality in fading + LOS](0013-spark-cpu-iq-equality.md) | Reproducible existing test failure |
| [#14](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/14) | [Qualify continuous moving traffic across two gNBs and two srsUEs](0014-moving-two-cell-continuity.md) | Failed live qualification |
| [#15](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/15) | [Validate fractional-delay continuity at live hardware block boundaries](0015-fractional-delay-hardware-boundary.md) | Known processing boundary and unqualified hardware behavior |
| [#16](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/16) | [CMX500/X310: resolve late uplink timing and validate IP traffic](0016-cmx-uplink-timing.md) | Measured hardware timing failure and incomplete IP qualification |
| [#17](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/17) | [Qualify live downlink activity on every claimed gNB antenna branch](0017-active-multiport-downlink.md) | Live evidence coverage gap |
| [#18](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/18) | [Spark: CPU-PHY UE scaling falls below real-time rate at 8 and 16 UEs](0018-spark-cpu-ue-scaling.md) | Measured capacity limitation |
| [#19](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/19) | [Preserve channel continuity across Sionna delay/layout changes](0019-geometry-update-continuity.md) | Known channel-update limitation |

## Recorded closed issues

| GitHub | Local record | Resolution |
|---|---|---|
| [#20](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/20) | [Scheduler launcher truncates multiline broker environment settings](0020-multiline-broker-environment.md) | Fixed and verified in [fd0e61a](https://github.com/zhouyou-gu/ocudu-gpu-channel/commit/fd0e61a37c0e4972ef986231088413fac9fa8d7a) |

## Maintaining a record

Record the component and platform, observed versus expected behavior, reproduction scope, confirmed findings versus unknowns, concise evidence links and closure checks. Update the GitHub issue and local record when findings, fixes or validation change; link the fix and verification before closing. Unpublished fixes remain distinguishable from delivered changes.

Keep bulk logs, raw traces, IQ captures, credentials and private host details out of both locations. Detailed artifacts stay in ignored validation storage; link compact reports and evidence identifiers instead. Preserve closed records and their links rather than deleting the history. If GitHub is unavailable, describe the pending record and blocker locally without inventing an issue number.

The initial records cover the documented open failures and evidence limits on the [status page](../getting_started/status.md). Creating this tracker added no live qualification and did not restart the stopped experiments. Measured reports retain their own dates and setups; a tracked qualification gap is not automatically a newly discovered software defect.

```{toctree}
:maxdepth: 1
:hidden:

0011-rtx-slot-delays
0012-spark-periodic-slot-gaps
0013-spark-cpu-iq-equality
0014-moving-two-cell-continuity
0015-fractional-delay-hardware-boundary
0016-cmx-uplink-timing
0017-active-multiport-downlink
0018-spark-cpu-ue-scaling
0019-geometry-update-continuity
0020-multiline-broker-environment
```
