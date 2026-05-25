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
| `gpu-test-sequence.sh` | **The locked-in 7-step GPU validation.** Build → ctest → clean relay → AWGN relay → 3-node graph → 2-cell multi-gNB → TDL-A profile. Must pass before any broker/CUDA change ships. |

## OCUDU + srsRAN smokes

| Script | Milestone | Topology |
|---|---|---|
| `ocudu-attach-smoke.sh` | A | 1 gNB + 1 UE, attach + ping verification |
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

## Notes

Use Wi-Fi only for SSH/control unless a wired low-latency data path has been validated. Distributed IQ transport requires the network criteria in [`docs/distributed.md`](../../docs/distributed.md).
