# #16: CMX500/X310: resolve late uplink timing and validate IP traffic

GitHub: [#16](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/16) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** UHD–ZMQ hardware bridge and srsUE transmit scheduling

**Platform:** CMX500 + USRP X310 + CUDA broker + srsUE, recorded hardware setup

**Category:** Measured hardware timing failure and incomplete IP qualification

## Observed and expected behavior

Recorded connected-mode uplink margin was about −0.1 ms versus the X310 requirement of 0.2 ms; approximately 66% of uplink blocks were late and about half the samples were not sent. Attachment/PDU worked, but ping/iperf were not validated in the recorded container without TUN.

Send uplink blocks before the required hardware deadline and demonstrate actual IP traffic through the declared attached setup.

## Reproduction and scope

Use the pinned CMX500/X310 bridge configuration and timing CSV analysis in use_cases/cmx500. Preserve the RF/sample-rate setup, record timed-transmit outcomes and provide a working TUN path for IP tests.

## Findings and unknowns

The recorded GPU channel processing took about 45 µs per 0.5 ms block; this alone does not explain the end-to-end uplink deficit. Native software-radio timing controls are not a hardware qualification.

A validated correction to the uplink scheduling/transport margin and an end-to-end IP result remain outstanding. The dated contributor measurements were not rerun during the November review.

## Closure checks

Demonstrate the required hardware transmit margin without concealed skipped samples and pass declared packet delivery tests over the actual CMX/X310 path, with pinned patches and timing evidence.

## Evidence

[use_cases/cmx500/README.md](../../use_cases/cmx500/README.md), [docs/getting_started/status.md](../getting_started/status.md).

[Issue index](README.md)
