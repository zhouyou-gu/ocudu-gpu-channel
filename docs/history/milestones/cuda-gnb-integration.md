> Historical integration record from `docs/cuda-ocudu-integration.html` at `58d3156`. Current procedures are in the operating guides.

Implementation report · ocudu-gpu-channel · branch cuda-rebuild

# CUDA-accelerated OCUDU gNB on a discrete GPU

How the working group's CUDA gNB is integrated into this project as a selectable profile: its components, its configuration surface, which PHY paths execute on the device, the compatibility patch it requires, and what the verification chain proves.

Vendor sourcenvcuda_accel_02 @ 5830c9cb Compatibility patchsha256 d2579af2… gNB binarysha256 063e6cb9… Buildbuilds/c1-cuda-patched · arch 120 GPURTX 5090 · sm_120 · discrete Driver / CUDA595.71.05 / 12.8.93 1 · Purpose and shape

## What the integration is

Before this work the channel emulator and Sionna RT ran on the GPU while the gNB did not: it received IQ over ZMQ and ran its PHY on the CPU. The integration puts the gNB's physical layer on the same GPU, so that *broker channel → lower-PHY OFDM → PUSCH equalisation and LDPC → LLR* is one device-resident pipeline rather than a sequence of host round trips.

The CUDA gNB is a **selectable additional profile**. The pinned CPU build remains the reference and the regression net; nothing about the existing gate's behaviour changes unless a caller opts in.

### Runtime topology

                        ┌──────────────────────────────── one RTX 5090 ────────────────────────────────┐
                        │                                                                               │
       srsUE ◀──ZMQ──▶  │  ocudu-gpu-channel (broker)          gNB PHY                                  │
                        │  ├ per-edge channel kernel           ├ lower-PHY RX   device grid writer      │
                        │  ├ H2D / kernel / D2H                ├ lower-PHY TX   host-staged read        │
                        │  └ ZMQ relay, 1 ms slot budget       ├ PUSCH          resident LDPC decoder   │
                        │                                      ├ PDSCH          device grid writer      │
       Open5GS 5GC ◀────┤                                      ├ PRACH          CUDA detector           │
                        │                                      └ SRS            CUDA estimator          │
                        └───────────────────────────────────────────────────────────────────────────────┘

2 · Components

## What was added, and where

Everything CUDA-specific is a new file under `scripts/cuda/`. The pre-existing gate scripts take four environment overrides and are otherwise unchanged (`+67/−17` across two files); the artifact verifier and the shared workspace lock are untouched.

<table>
<colgroup>
<col style="width: 50%" />
<col style="width: 50%" />
</colgroup>
<thead>
<tr>
<th>Component</th>
<th>Role</th>
</tr>
</thead>
<tbody>
<tr>
<td>cuda-workspace.lock.json</td>
<td>Pins the vendor source revision, the patch path and its sha256, and the build directory and CUDA architecture. Separate from the shared workspace lock, which the existing gates read.</td>
</tr>
<tr>
<td>resolve-cuda-gnb.py</td>
<td>Pre-flight audit. Verifies source HEAD against the lock, that the working tree diff is byte-identical to the locked patch, the CMake cache (<code>ENABLE_CUDA</code>, arch 120, ZeroMQ), the binary's reported revision, and that <code>--help</code> advertises GPU acceleration. Fails loudly rather than falling back to the CPU build.</td>
</tr>
<tr>
<td>run-ocudu-cuda-1x1.sh</td>
<td>Launcher. Audits, then hands the existing gate the binary path, its revision, and the CUDA-specific timing allowances. The gate learns nothing else.</td>
</tr>
<tr>
<td>render-cuda-1x1-configs.py</td>
<td>Config composition. Runs the shipping renderer through the gate's existing <code>OCUDU_NATIVE_CONFIG_RENDERER</code> hook, then appends the acceleration block. With no stage selected, output is byte-identical to a plain render.</td>
</tr>
<tr>
<td>patches/c1-…patch</td>
<td>The single hash-locked vendor patch. See §5.</td>
</tr>
<tr>
<td>c0-build-and-validate.sh<br />
c1-build.sh · c1-validate.sh</td>
<td>Unpatched qualification build and validation; patched build and validation. Separate build directories so the unpatched evidence is never overwritten.</td>
</tr>
<tr>
<td>c1-negative-controls.sh<br />
d5-negative-control.sh</td>
<td>Restore one defect at a time, rebuild, and require the corresponding test — or, for D5, the live gate — to fail.</td>
</tr>
<tr>
<td>verify-phy-log.py</td>
<td>Log scorer. The vendor's comparison and pipeline tests can print a failure verdict and still exit 0, so ctest's status alone is not trusted.</td>
</tr>
<tr>
<td>verify-stage-backends.py</td>
<td>Scores a run against the runtime markers the gNB writes: did the accelerated path that was requested actually run, degrade to another GPU path, or fall back to the host.</td>
</tr>
<tr>
<td>summarize-phy-kpis.py<br />
summarize-broker-metrics.py</td>
<td>Extract BLER, SINR and processing time from the gNB log, and latency percentiles, counters and GPU sub-phase timings from the broker log. No new instrumentation — both read what the run already writes.</td>
</tr>
<tr>
<td>c4-staged-runs.sh · c4-bler-run.sh<br />
compare-c4-stages.py</td>
<td>Staged activation sweep, traffic-bearing BLER pair, and the comparator that applies C4's numeric tolerances.</td>
</tr>
<tr>
<td>c5-paired-measurement.sh<br />
with-cuda-mps.py · sample-run-telemetry.py<br />
report-c5-envelope.py</td>
<td>Paired CPU/CUDA measurement under one MPS server and one CPU affinity mask, with the mask each thread actually carries read from <code>/proc</code>, and the envelope report.</td>
</tr>
</tbody>
</table>

3 · Configuration surface

## How acceleration is selected

Two layers of the gNB YAML carry acceleration keys, and the resource grids have their own memory-mode selectors. The launcher exposes a single ordered stage name that expands into a cumulative set of them.

### gNB configuration keys

| Key | Section | Selects |
|----|----|----|
| pusch_acceleration_mode | expert_phy | CUDA PUSCH demodulator and resident LDPC decoder |
| pdsch_acceleration_mode | expert_phy | CUDA PDSCH block processor |
| prach_acceleration_mode | expert_phy | CUDA PRACH detector |
| srs_acceleration_mode | expert_phy | CUDA SRS estimator |
| ul_cuda_visible_grid_mode | expert_phy | Uplink grid memory: `managed` / `pinned` / `auto` |
| dl_cuda_visible_grid_mode | expert_phy | Downlink grid memory, same values |
| low_phy_rx_acceleration_mode | ru_sdr.expert_cfg | CUDA OFDM demodulation into the uplink grid |
| low_phy_tx_acceleration_mode | ru_sdr.expert_cfg | CUDA OFDM modulation from the downlink grid |
| low_phy_prach_demodulation_acceleration_mode | ru_sdr.expert_cfg | CUDA PRACH demodulation into the PRACH buffer |

auto is not "on"

Every selector defaults to `auto`, and on a discrete device `auto` prefers the host for PDSCH and selects `pinned` for the uplink grid. Turning a path on requires `enabled` explicitly, and forced lower-PHY acceleration needs the managed grid's device mapping — which is why the stage expansion raises the grid modes with the rungs.

### Stage expansion — `OCUDU_NATIVE_GNB_ACCELERATION`

| Stage | Adds | UL grid | DL grid |
|----|----|----|----|
| disabled | nothing — the CUDA binary running its host PHY (parity control) | pinned | pinned |
| low-phy-rx | lower-PHY RX | managed | pinned |
| low-phy-tx | \+ lower-PHY TX | managed | managed |
| pusch | \+ PUSCH | managed | managed |
| pdsch | \+ PDSCH | managed | managed |
| prach | \+ PRACH and lower-PHY PRACH demodulation | managed | managed |
| all | \+ SRS | managed | managed |

### Gate environment overrides

| Variable | Default | Effect |
|----|----|----|
| OCUDU_NATIVE_GNB_BINARY | audited CPU build | Which gNB the gate runs. The path was hardcoded in six places. |
| OCUDU_NATIVE_GNB_COMMIT | a1916ed | Which revision that binary must report. Required with an override, so the gate's identity check stays a real check. |
| OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS | 15 | Wait for the started banner. CUDA device initialisation runs before it — measured at 19.7 s with every mode enabled, against 1.1 s for the CPU build. |
| OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS | 0 | Added to the broker's own `--duration`. That clock starts with the broker process, not with the gNB's radio, so without an allowance the broker's window expires during CUDA initialisation and no IQ ever flows. |
| OCUDU_NATIVE_UE_KEEPALIVE_SECONDS | 0 | Opt-in keepalive ping, needed to accumulate enough PUSCH transmissions for a BLER comparison. Writes to its own log; the acceptance check still reads the acceptance ping alone. |

With all five unset, every expression resolves to the literal value the scripts held before the workstream existed — an unset run *is* the pre-change run, verified by rendering both configurations and diffing them.

4 · Execution paths

## What actually runs on the device

At `acceleration=all`, confirmed from the runtime manifests the gNB emits rather than from configuration. A requested path that quietly runs on the host still attaches and pings, so the manifests are the evidence, not the verdict of the gate.

| Path | Executes on | Runtime manifest |
|----|----|----|
| Lower-PHY RX · OFDM demodulation | device | direct CUDA-visible uplink resource-grid writer |
| Lower-PHY TX · OFDM modulation | device, host-staged | host resource-grid staging fallback — see §7 |
| Lower-PHY PRACH demodulation | device | direct CUDA-visible PRACH buffer writer |
| PUSCH · demodulation, equalisation, LDPC | device | backend=CUDA resident_decode=true decoder_pool=5 |
| PDSCH · encode, modulate, grid write | device | backend=CUDA lanes=1 device_grid_writer=true |
| PRACH detection | device | backend=CUDA |
| SRS estimation | device | resolved=enabled backend=CUDA |
| PDCCH, SSB/PBCH, PUCCH | host | not accelerated in this vendor branch |

Consequence for the objective

The uplink chain — the direction that grows with users × receive antennas × LLR — is device-resident end to end. That is the path C7's device-LLR hook attaches to. The downlink grid is the one place a host copy remains, and it does not scale with user count.

5 · Vendor compatibility patch

## What the patch changes, and why each change is required

The vendor branch builds cleanly here but scores **9 of 12** on the validation its own documentation prescribes. The patch is a single hash-locked file — seven production files `+88/−35`, four test files `+64/−13`, no SPDX header touched. It is carried locally and reported upstream; every claim derived from the build declares that the patch is applied.

| ID | Site | Condition | Change | Control |
|----|----|----|----|----|
| D1 | srs_estimator_cuda_impl.cpp | Hard-requires a snapshot API that only the managed grid implements; a discrete GPU runs a pinned or host grid, where it is `nullptr` | Branch on `supports_device_grid_reading()`; managed keeps the vendor path, host/pinned stage through the existing device grid reader | exit 8 |
| D2 | iq_compression_cuda.cpp | Device residency gate refuses a live read after a host consumer migrated the grid; the caller returns false instead of falling back | Fall back to the owned snapshot, which the vendor's own comment specifies, and release whichever hold was taken | exit 8 |
| D3 | factories.h · upper_phy_factories.cpp · pusch_demodulator_gpu_impl | The accelerated demodulator configuration has no time-interpolation field, so the CPU interpolates while the GPU averages. Hardware-independent. | Add the field, carry it through the constructor, and log the mode actually applied | exit 8 |
| D4 | pusch_demodulator_gpu_impl.cpp | The synchronous path substitutes an EVM-derived value into `sinr_dB`; the file's three other emission sites do not. Hardware-independent. | Report the post-equalisation noise-variance SINR, keep EVM in its own field | exit 8 |
| D5 | upper_phy_factories.cpp | Accelerated PDSCH requires a codeblock executor that the ZMQ sequential PHY deliberately does not supply; the two are mutually exclusive and the gNB aborts during construction | Substitute an inline executor when acceleration is requested, the configuration is flexible, and no executor exists — forced CUDA PDSCH already batches synchronously, so no task queue is introduced. The substitution is logged. | exit 2 |

### Two causes, not one

D1 and D2 are discrete-GPU conditions: the vendor validated on unified memory, where the grid is always managed and the API they require always exists. D3 and D4 are hardware-independent latent bugs that reproduce on their own platform. D5 is neither — it is a property of driving the gNB with a ZMQ radio.

### Why the suite cannot see D3 and D4

`pusch_gpu_cpu_comparison_test` pins both sides to `average` interpolation, sets CFO to zero, and exercises only the deferred path. The added regressions vary exactly those three: differing config, non-zero CFO, and the synchronous path. D3 additionally cannot be tested before it is fixed — a test that asks the GPU for a different strategy needs the field whose absence is the defect.

Severity note — D3

With a default gNB configuration and non-zero CFO, the CPU reference reaches `0 %` BLER while the GPU path reaches `100 %` and decode agreement is `0 %`. It surfaced here as a live failure: attach succeeded, then PDU session establishment failed with PUSCH CRC 165 KO / 1 OK.

6 · Verification chain

## What each gate proves

| Check | What it establishes | Result |
|----|----|----|
| resolve-cuda-gnb.py | The binary under test is built from the locked source with the locked patch, for the right architecture, and reports the right revision | clean |
| c1-validate.sh | The patched build passes the vendor's twelve PHY tests plus the two added regressions, and the OFH compression suite | PHY 14/14 · OFH 16/16 |
| negative controls | Each fix is the *reason* its test passes: restoring the defect must fail, and for D5 the intended fatal error must appear in the log | 5/5 fail as required |
| pre-existing 1×1 gate | With no environment set, the CPU path is unchanged — rendered configs byte-identical, verdict fields identical, binary hash identical | no regression |
| CUDA gate, `disabled` | Parity control: the CUDA binary running its host PHY attaches, establishes a PDU session and pings, separating "the build swap broke it" from "the acceleration broke it" | pass |
| c4-staged-runs.sh | Every rung of the ladder passes, and each rung's requested path is confirmed selected from the runtime log rather than inferred from survival | 8/8 · no host fallback |
| c4-bler-run.sh | Uplink BLER and SINR against the CPU build, on a run long enough for the measurement to resolve the tolerance it is judged by | within 0.073 pp · 0.173 dB |
| c5-paired-measurement.sh | Paired arms in one envelope — same MPS server, same CPU affinity verified from `/proc` — with latency, transfer time and GPU occupancy recorded | no regression |

7 · Measured characteristics

## Behaviour of the integrated system

Performance-claim boundary

At this fixture — 20 MHz, one layer — the working group's own figures put the GPU at `0.86×` on PUSCH and `0.37×` on PDSCH. **The GPU being slower here is expected and is not a failure.** The figures below are recorded as an envelope and a regression check; no speed claim is made before C7, where bandwidth and layer count rise.

### Link quality against the CPU build

| Arm             | PUSCH tx | BLER  | Resolution | SINR mean | Δ SINR |
|-----------------|----------|-------|------------|-----------|--------|
| CPU baseline    | 1372     | 0.0 % | ±0.073 pp  | 1.386 dB  | —      |
| CUDA, all modes | 998      | 0.0 % | ±0.100 pp  | 1.559 dB  | +0.173 |

Tolerances are 1 pp and 0.5 dB. Across the staged ladder the worst SINR deviation is `+0.200 dB`. The BLER result shows the accelerated path does not *introduce* errors over roughly a thousand transmissions; it does not discriminate between the paths under stress, because legacy channel mode relays IQ close to cleanly and both arms read 0 %.

### Timing envelope — paired arms, MPS, P-core pinned

<table>
<thead>
<tr>
<th>Measure</th>
<th>CPU arm</th>
<th>CUDA arm</th>
<th>Reading</th>
</tr>
</thead>
<tbody>
<tr>
<td>Broker per-slot latency p50 / p95 / p99</td>
<td>62.5 / 70 / 75 µs</td>
<td>65 / 70 / 75 µs</td>
<td>No regression in the relay path</td>
</tr>
<tr>
<td>Broker last-batch H2D</td>
<td>16.8 µs</td>
<td>16.1 µs</td>
<td rowspan="2"><strong>Attaching the CUDA gNB does not increase the broker's PCIe transfer time</strong></td>
</tr>
<tr>
<td>Broker last-batch D2H</td>
<td>9.3 µs</td>
<td>9.7 µs</td>
</tr>
<tr>
<td>gNB PUSCH processing p50</td>
<td>61.1 µs</td>
<td>142.5 µs</td>
<td>Slower, as the boundary above expects at this fixture</td>
</tr>
<tr>
<td>GPU SM clock, mean</td>
<td>1923–2080 MHz</td>
<td>2363–2364 MHz</td>
<td>The arms do not sit at the same clock, so broker kernel time is not compared</td>
</tr>
<tr>
<td>Late or dropped slots · overflows · sequence gaps · ZMQ errors</td>
<td>0</td>
<td>0</td>
<td>Clean on every run</td>
</tr>
</tbody>
</table>

8 · Known limits

## The downlink grid cannot stay device-resident

Lower-PHY TX reads the downlink grid from a host-staged copy rather than directly on the device. It is still CUDA, and it is not patchable: the direct reader requires all three managed-memory coherence attributes, and this GPU satisfies one.

<table>
<colgroup>
<col style="width: 20%" />
<col style="width: 20%" />
<col style="width: 20%" />
<col style="width: 20%" />
<col style="width: 20%" />
</colgroup>
<thead>
<tr>
<th>Platform</th>
<th>concurrent<br />
ManagedAccess</th>
<th>hostPage<br />
Tables</th>
<th>directManaged<br />
AccessFromHost</th>
<th>Qualifies</th>
</tr>
</thead>
<tbody>
<tr>
<td>RTX 5090 measured</td>
<td>1</td>
<td>0</td>
<td>0</td>
<td>no</td>
</tr>
<tr>
<td>GB10 from vendor table</td>
<td>1</td>
<td>1</td>
<td>0</td>
<td>no</td>
</tr>
<tr>
<td>GH200-class</td>
<td>1</td>
<td>1</td>
<td>1</td>
<td>yes</td>
</tr>
</tbody>
</table>

Without full coherence, every host write to the downlink grid migrates its pages home and clears device residency. PDCCH and SSB are not accelerated in this branch, so the CPU writes them into that same grid every slot: PDSCH makes the grid device-resident, the control channels pull it home, and the modulator's device read is then refused. `directManagedAccessFromHost = 0` means the CPU cannot touch GPU-resident managed pages without migrating them, so the only correct fix is a separate host buffer merged on the device — a rewrite of the writer interface every downlink channel processor uses, not a compatibility patch.

Reported upstream as an observation about the documentation, which describes `low_phy_tx_acceleration_mode` without this distinction while spelling out discrete behaviour for the neighbouring PDSCH selector. Cost at this fixture is roughly 70 KB of staging per slot.

9 · Status

## Milestones

| Milestone | State | Scope |
|----|----|----|
| C0 · qualify vendor branch | complete | unpatched build scored 9/12 against the vendor's own validation |
| C1 · patch and controls | complete | five defects, hash-locked; upstream report written, **not submitted** |
| C2 · provisioning | complete | lock, resolver, build scripts — all new files |
| C3 · additive CUDA gate | complete | parity control passes; pre-existing gate byte-identical when unset |
| C4 · staged activation | complete | 8/8 rungs, backends confirmed, BLER and SINR within tolerance |
| C5 · measurement | complete | paired envelope recorded, no regression |
| C6 · concurrent Sionna | not started | broker + Sionna RT + CUDA gNB on one GPU, kernel concurrency under nsys |
| C7 · device LLR exposure | not started | read equalised symbols and LLRs on the device; per-stage GPU timing in the web UI. **The objective, and the first point where a speed claim is in scope.** |

10 · Operations

## Running and re-verifying

    source /home/minwoo/ocudu-env.sh
    export OCUDU_REPO=/home/minwoo/ocudu-work/ocudu-cuda-rebuild   # the default points at the other tree
    cd "$OCUDU_REPO"

    # CPU reference — no acceleration environment set
    bash scripts/native/run-ocudu-legacy-1x1.sh

    # CUDA profile — any stage from the expansion table in §3
    OCUDU_NATIVE_GNB_ACCELERATION=all bash scripts/cuda/run-ocudu-cuda-1x1.sh

    # verification chain
    python3 scripts/cuda/resolve-cuda-gnb.py --root "$OCUDU_NATIVE_ROOT"   # needs scripts/native/env.sh sourced
    bash scripts/cuda/c1-build.sh
    bash scripts/cuda/c1-validate.sh            # PHY 14 + OFH 16, about 20 minutes
    bash scripts/cuda/c1-negative-controls.sh
    bash scripts/cuda/d5-negative-control.sh
    bash scripts/cuda/c4-staged-runs.sh
    bash scripts/cuda/c4-bler-run.sh
    bash scripts/cuda/c5-paired-measurement.sh

    # restore the patched source state if the tree was reset
    cd "$OCUDU_NATIVE_ROOT/src/ocudu-cuda"
    git checkout -- . && git apply "$OCUDU_REPO/integrations/ocudu/patches/c1-ocudu-cuda-discrete-and-config.patch"
    git diff --binary HEAD | sha256sum          # must read d2579af2…

Evidence lives in the native workspace under `results/cuda-rebuild/` and `results/{logs,reports}/ocudu-interop/` — IQ captures, logs and binaries are not in git. Canonical roadmap: `CUDA_MILESTONES.md` · canonical state: `AGENT_PROGRESS.md` · resume summary: `HANDOVER-CUDA.md`
