# `scripts/remote/`

Reproducible workflows that run on the RTX workstation. Every script sources
`.config` via `common.sh` and never embeds private host values.

## Workspace + toolchain

| Script | Purpose |
|---|---|
| `init-workspace.sh` | Create the remote workspace directory tree once. |
| `bootstrap-user-tools.sh` | Install user-space CMake / CUDA Toolkit / ZeroMQ under `~/ocudu-gpu-channel-workspace/tools/`. No root. |
| `probe.sh` | Sanity-check the remote toolchain (cmake, nvcc, nvidia-smi, ZeroMQ). |
| `sync.sh` | rsync the local working tree to the remote project root. |
| `common.sh` | Shared sourcing (sourced by every other script). |

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
| `ocudu-multi-ue-smoke.sh` | B | 1 gNB + 2 UEs on one cell |
| `ocudu-multi-gnb-smoke.sh` | C | 2 gNBs + 2 UEs, inter-cell interference |
| `ocudu-interop-smoke.sh` | — | Broader OCUDU interop sanity |

## Perf sweeps

Three sweep scripts with overlapping but distinct scopes:

| Script | Backend(s) | Configs | What it measures | When to use |
|---|---|---|---|---|
| `perf-sweep.sh` | CPU + CUDA | Every YAML in `examples/` | Per-phase latency + throughput + memory per backend per config. Populates the §21 perf table. | Wide regression sanity after a backend change. |
| `perf-fanin-sweep.sh` | CUDA only | 21 generated one-to-N configs (N = 1, 2, 4, 8, 16, 32) plus TDL profile fan-ins | Per-config bench latency + per-phase GPU µs + `nvidia-smi` snapshot + memory delta + a short `nsys` trace per config. | Fan-in scaling curves (Diagram T / Diagram U); kernel-level timing dives. |
| `perf-backend-compare.sh` | CPU vs CUDA | Synthetic fan-in (one-to-N for N = 1, 4, 16) + TDL-A..E profiles | Per-config p99 latency for both backends + per-RX-node cumulative `avg_power` for CPU↔CUDA matching verification. | Verify CPU↔CUDA parity and quantify speedup at the same time. |
| `perf-deep-profile.sh` | CUDA | One config (default: `topology.stress-16-edge.cuda.yaml`) | PCIe throughput, host + device memory, SM utilisation, `ncu` kernel metrics where accessible. | Drill into a single config's resource bottleneck. |

Output of every sweep lives under `~/ocudu-gpu-channel-workspace/results/<sweep_name>/<timestamp>/` on the remote.

## Which harness is authoritative

`scripts/remote/` is the supported live-test path and the one every live claim
should cite. It provisions its own Docker network and 5GC, selects a free
subnet instead of assuming one, and self-provisions the Python it needs, so a
clone plus this host is enough to reproduce every live gate.

`scripts/native/` is a rootless, user-namespace harness inherited from the
rank-1 workstream. It is retained because its verification tooling is shared --
`verify-mimo-matrix-capture.py` is the checker the Docker gates call -- and
because its fixtures document the gNB constraints. It is **not** reproducible
from this repository: `bootstrap-workspace.sh` has build provisioning disabled
by design, so the workspace it expects (OCUDU, srsRAN_4G, Open5GS and MongoDB
at pinned revisions under `~/ocudu-native-workspace`) has to be produced by a
process that is not committed here, and its scripts hard-code `/home/ubuntu`
and `/opt/conda` paths. Treat the native gate scripts as a record of how the
original runs were performed, not as a path a reader can execute.

The rank-1 gates above are the Docker ports of `scripts/native/run-ocudu-rank1-*.sh`.
They use the same channel matrices, the same gNB cell configuration and the same
matrix checker; only the ZMQ endpoint form and the container plumbing differ.

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

Use Wi-Fi only for SSH/control unless a wired low-latency data path has been validated. Distributed IQ transport requires the network criteria in [`docs/distributed.md`](../../docs/distributed.md).
