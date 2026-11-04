# #14: Qualify continuous moving traffic across two gNBs and two srsUEs

GitHub: [#14](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/14) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** Moving Sionna scenario and native multi-gNB stack integration

**Platform:** Two-cell/two-UE native runtime; recorded RTX validation

**Category:** Failed live qualification

## Observed and expected behavior

The recorded 600-second moving two-cell run did not maintain continuous traffic for both UEs; one UE had repeated zero-reply batches and no final ping replies. Later successful single-cell/two-UE delivery does not clear this two-cell failure.

Meet the documented moving two-cell connectivity/recovery checks for both UEs, with serving-cell and channel coverage evidence.

## Reproduction and scope

Use the recorded moving two-cell SUTD scenario and pinned native multi-gNB setup; preserve the route and topology. Measure both UE traffic streams, both scheduler feeds and actual serving PCIs.

## Findings and unknowns

Static two-cell attachment and traffic pass, while continuous moving two-cell operation remains unqualified. Rank-1 single-cell success is a separate result.

Separate legitimate physical loss of coverage from stack recovery defects. Seamless handover and full NR radio-link recovery have not been qualified; attachment alone does not prove them.

## Closure checks

Explain each outage using coverage and stack evidence, correct reproducible software faults, and pass the declared moving two-cell traffic/recovery checks for both UEs without substituting a stationary or single-cell scenario.

## Evidence

[docs/history/milestones/sionna-integration-20260914.md](../history/milestones/sionna-integration-20260914.md), [docs/reports/validation/sionna-merge-fixes-validation.md](../reports/validation/sionna-merge-fixes-validation.md), [docs/getting_started/status.md](../getting_started/status.md).

[Issue index](README.md)
