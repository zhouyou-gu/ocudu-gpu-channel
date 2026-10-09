# Rank-1 evidence boundaries

> Historical evidence migrated from the technical reference at `58d3156`. Measurements retain their original setups and limits; see [current status](../../getting_started/status.md) for the qualification summary.

(section-25)=

(mimo-evidence)=

## 25. Live evidence, and the line it stops at

What has actually been run against a real radio, and what that does **not** license anyone to claim.

The standing live gate is a real 2-antenna OCUDU gNB (four ZMQ endpoints, pinned revision, byte-pinned fixture) exchanging IQ with the CUDA broker and a two-port synthetic peer, with no Docker and no 5G core. Over a 20 s run it holds ~1 174 four-endpoint groups per second with every strict broker counter at zero, zero `Real-time failure in RF` in the gNB's own log, sibling reply sizes identical, and sibling TX acquisition skew of 0 samples.

Those are transport facts. On their own they would also be satisfied by a broker that passed each port straight through, so the gate additionally judges **what the emulator computed**. The broker records a bounded window of both wires per port — what it pulled off the peer's TX and what it replied with on its own RX, at the socket boundary — and an independent checker reads `H` from the topology YAML, recomputes `y`<sub>`r`</sub>` = Σ`<sub>`t`</sub>` H[r][t] x`<sub>`t`</sub>, and compares:

<table>
<thead>
<tr>
<th>Measured on the live gate</th>
<th>Downlink (real gNB signal)</th>
<th>Uplink (marked peer signal)</th>
</tr>
</thead>
<tbody>
<tr>
<td>max <code>|y − Hx|</code>, row 0 / row 1</td>
<td>4.1e−08 / 2.6e−08</td>
<td>1.5e−07 / 1.2e−07</td>
</tr>
<tr>
<td>tolerance</td>
<td colspan="2">1e−04 (the <code>fixed_mimo</code> dB/rad round trip costs ~1e−07)</td>
</tr>
<tr>
<td>share of row amplitude from the <strong>other</strong> transmit port</td>
<td>0.12 / 0.768</td>
<td>0.331 / 0.508</td>
</tr>
<tr>
<td>analytic marker mismatches, 230 400 samples/port</td>
<td>—</td>
<td>0</td>
</tr>
</tbody>
</table>

The last two rows are the ones that carry the MIMO claim. The off-diagonal share makes "each received row depends on both transmit ports" a measurement rather than an assertion: a relay running two independent 1×1 lanes scores zero there. The marker check compares the captured uplink columns against a closed form of `(port, ordinal)`, which pins the common sample epoch — sibling columns read from different origins fail it even when the matrix arithmetic on them is self-consistent.

Three mutation probes confirm the check fails when it should: a **diagonal** matrix fails all four rows (0.052–0.284); **one lane removed** fails row 1 in both directions while row 0 still matches at 1e−07, so detection is localised to the removed lane; and skewing one sibling column's epoch by a **single sample** fails all four rows while the markers still pass.

**The claim boundary, stated as plainly as the code states it.** The gate's own report carries `transport_only: true`, `ue_decode: false`, `rank2_claim: false`. A live rank\>1 link needs a UE PHY that jointly estimates and decodes a matrix channel, and the srsUE build this project integrates does not: its NR receiver reads antenna 0 only (`ue_dl_nr.c` passes `sf_symbols[0]` to PDCCH, PDSCH and CSI-RS estimation). Several independent single-port UE processes are not one multi-port UE. **Multi-port transport flow is not a rank-2 claim, and this project does not make one.** What is live-demonstrable today is a 2-port gNB whose ports the emulator combines through a declared matrix, and an uplink the gNB equalises across both of its receive ports.
