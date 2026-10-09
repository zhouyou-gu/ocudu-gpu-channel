# `scripts/remote/`

Reproducible workflows that run on the RTX workstation. Every script sources
`.config` via `common.sh` and never embeds private host values.

## Workspace + toolchain

| Script | Purpose |
|---|---|
| `init-workspace.sh` | Create the remote workspace directory tree once. |
| `bootstrap-user-tools.sh` | Install user-space CMake / CUDA Toolkit / ZeroMQ under `~/ocudu-gpu-channel-workspace/tools/`. No root. |
| `probe.sh` | Sanity-check the remote toolchain (cmake, nvcc, nvidia-smi, ZeroMQ). |
| `sync.sh` | Fetch and fast-forward the remote clone to the current branch on origin; local uncommitted changes are not uploaded. |
| `bootstrap-sionna.sh` | Install the optional bridge dependencies in the remote workspace’s `venvs/sionna`; requires a synchronized checkout. |
| `common.sh` | Shared sourcing (sourced by every other script). |

The GPU sequence, performance sweeps and selected smoke scripts upload the local working tree with rsync before running. Their shared `rsync-excludes.txt` keeps local settings, research, tooling and generated artifacts out of the upload; excluded remote files are preserved. Ordinary untracked source is included. This upload is separate from `sync.sh`’s Git update.

## Build + run

| Script | Purpose |
|---|---|
| `build-and-bench-cuda-mvp.sh` | Build the CUDA release, run the MVP benchmark, print latency summary. |
| `gpu-test-sequence.sh` | **The locked-in 9-step GPU validation.** Build → ctest → clean relay → AWGN relay → 3-node graph → 2-cell multi-gNB → TDL-A profile → 2x2 correlated MIMO → live control-plane swap. Must pass before any broker/CUDA change ships. |

## OCUDU + srsRAN smokes

| Script | Milestone | Topology |
|---|---|---|
| `ocudu-attach-smoke.sh` | A | 1 gNB + 1 UE, attach + ping verification |
| `ocudu-rank1-2x1-smoke.sh` | R2 | 2T2R gNB + 1-antenna srsUE: 2x1 DL MISO / 1x2 UL SIMO, attach + PDU + ping + live y=Hx |
| `ocudu-rank1-4x1-smoke.sh` | R3 | 4T4R gNB + 1-antenna srsUE: 4x1 DL MISO / 1x4 UL SIMO, attach + PDU + ping + live y=Hx |
| `ocudu-rank1-2x1-multi-ue-smoke.sh` | R2-MU | 2T2R gNB + **two** 1-antenna srsUEs: both uplinks superpose on the two gNB receive ports |
| `ocudu-rank1-4x1-multi-ue-smoke.sh` | R3-MU | 4T4R gNB + **two** 1-antenna srsUEs: both uplinks superpose on the four gNB receive ports |
| `ocudu-rank1-2x1-triple-ue-smoke.sh` | — | 2T2R + **three** srsUEs. **Blocked**: all three reach RRC, PDU unreliable |
| `ocudu-rank1-2x1-quad-ue-smoke.sh` | — | 2T2R + **four** srsUEs. **Blocked**: only the last-started UE attaches |
| `ocudu-multi-ue-smoke.sh` | B | 1 gNB + 2 UEs on one cell |
| `ocudu-multi-gnb-smoke.sh` | C | 2 gNBs + 2 UEs, inter-cell interference |
| `ocudu-interop-smoke.sh` | — | Broader OCUDU interop sanity |

Measured results for every gate above, including why the three- and four-UE
gates are blocked, are recorded in [`docs/reports/validation/live-gate-results.md`](../../docs/reports/validation/live-gate-results.md).
The three- and four-UE scripts are committed as reproducible investigations, not
as passing gates; **two UEs per cell is the supported multi-user configuration.**

The static two-cell gate uses one antenna per gNB, with TX ports 3000 and
3002. Sionna mode uses four antennas per gNB, with TX ports 3000/3002/3004/3006
and 3010/3012/3014/3016. Custom topologies and scenarios must match the selected
mode's antenna dimensions and endpoints.

The Milestone C script also runs directly on a GPU host without SSH or
`.config`. The local launcher enables the Sionna RT channel by default:

```bash
./scripts/local/ocudu-gnb-ue-sionna-smoke.sh
```

It starts Open5GS, two OCUDU gNBs, two srsUEs, the CUDA broker, Sionna RT, and
the telemetry subscriber. Success requires both cells to activate and both UEs
to complete RRC attach, PDU-session setup, and ping. Results are written under
`results/reports/ocudu-multi-gnb/<timestamp>/` and logs under
`results/logs/ocudu-multi-gnb/<timestamp>/`.

## Perf sweeps

Four sweep scripts with overlapping but distinct scopes:

| Script | Backend(s) | Configs | What it measures | When to use |
|---|---|---|---|---|
| `perf-sweep.sh` | CPU + CUDA | Every YAML in `use_cases/` | Per-phase latency + throughput + memory per backend per config. Populates the §21 perf table. | Wide regression sanity after a backend change. |
| `perf-fanin-sweep.sh` | CUDA only | 21 generated one-to-N configs (N = 1, 2, 4, 8, 16, 32) plus TDL profile fan-ins | Per-config bench latency + per-phase GPU µs + `nvidia-smi` snapshot + memory delta + a short `nsys` trace per config. | Fan-in scaling curves (Diagram T / Diagram U); kernel-level timing dives. |
| `perf-backend-compare.sh` | CPU vs CUDA | Synthetic fan-in (one-to-N for N = 1, 4, 16) + TDL-A..E profiles | Per-config p99 latency for both backends + per-RX-node cumulative `avg_power` for CPU↔CUDA matching verification. | Verify CPU↔CUDA parity and quantify speedup at the same time. |
| `perf-deep-profile.sh` | CUDA | One config (default: `topology.stress-16-edge.cuda.yaml`) | PCIe throughput, host + device memory, SM utilisation, `ncu` kernel metrics where accessible. | Drill into a single config's resource bottleneck. |

Output of every sweep lives under `~/ocudu-gpu-channel-workspace/results/<sweep_name>/<timestamp>/` on the remote.

## Which harness is authoritative

`scripts/remote/` is the supported live-test path and the one every live claim
should cite. It provisions its own Docker network and 5GC, selects a free
subnet instead of assuming one, and self-provisions the Python it needs, within each gate's pinned-image and host prerequisites.

`scripts/native/` provides a rootless, user-namespace workflow and the matrix checker shared with the Docker gates. Its `bootstrap-workspace.sh` can download locked inputs, create a user-space dependency overlay and build the native stack; `--verify-only` audits an existing workspace and `--root` selects its location. This is an audited Ubuntu 24.04 host contract, not a hermetic clean-host build: the base compiler/runtime and 48 Debian dependency clauses are supplied by the host. See [native preparation](../native/README.md) and the [platform guide](../../docs/guides/platforms.md) before choosing this route.

The rank-1 gates above are the Docker ports of `scripts/native/run-ocudu-rank1-*.sh`.
They use the same channel matrices, the same gNB cell configuration and the same
matrix checker; only the ZMQ endpoint form and the container plumbing differ.

## Multi-user rank-1 gates

The two `-multi-ue-` gates put two srsUEs on one multi-antenna cell. Each UE
still has exactly one antenna, so each user's downlink is a 1xNt row and each
uplink an Ntx1 column -- the claims stay rank-1 MISO/SIMO and nothing here is
2x2.

What they add is **superposition at a multi-port receiver**: both uplinks arrive
on the same gNB receive ports, so every gNB RX row is the sum of both users'
columns. The matrix checker reconstructs each row from all incoming links, and
then removes one link at a time and requires the match to break -- a relay that
served only one user, or that summed the wrong pair, cannot pass.

Claim boundary: this is two single-layer users sharing a cell **in time**, not
same-PRB MU-MIMO. The emulator superposes whatever the scheduler actually
transmits; it does not make the gNB serve both users on the same resources.

## Live rank-1 gates: what they do and do not prove

Both rank-1 gates score `y = Hx` on the captured wire in the same run that
carried the attach. The two directions do not carry the same weight:

- **Uplink is genuinely multi-branch.** Each gNB receive port takes its own
  declared coefficient and is scored on its own row -- two rows for the 2x1
  gate, four for the 4x1 gate.
- **Downlink is single-branch.** srsRAN radiates SSB and common channels on
  port 0 only, and with CSI-RS disabled it precodes rank-1 PDSCH as [1, 0, ...],
  so the higher gNB TX ports carry no signal. The gates declare this with
  `--allow-silent-source`, which records the fact rather than relaxing the
  check: the downlink row still has to satisfy `y = h0*x0` exactly, and the
  silent ports still have to contribute exactly zero. It is not evidence of
  live multi-branch downlink combining. That evidence comes from the synthetic
  gates and the oracle-precoding experiment.

## Notes

Use Wi-Fi only for SSH/control unless a wired low-latency data path has been validated. Distributed IQ transport requires the network criteria in [`docs/guides/distributed.md`](../../docs/guides/distributed.md).
