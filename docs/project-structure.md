# Project Structure

The local repository is the canonical Git source. The remote RTX workstation is a reproducible validation mirror for GPU builds, OCUDU integration, and benchmark runs.

## Repository responsibilities

| Location | Owns |
|---|---|
| `apps/` | Executable entrypoints, the Sionna bridge and dashboard with browser assets |
| `include/`, `src/` | C++ interfaces and broker / CPU / CUDA implementations |
| `integrations/` | External-stack patches, exact revision pins and the UHD–ZMQ bridge |
| `scripts/` | Workspace preparation, launch orchestration and validation |
| `use_cases/` | Scenario code, launchers, analysis and shared `configs/` |
| `tests/` | Correctness checks grouped by component |
| `benchmarks/` | Standalone performance measurements |
| `docs/` | Current guides, measured reports and development plans/history |
| `archive/` | Historical records |

External source checkouts, build outputs, logs, captures and datasets remain outside tracked source. Private workstation settings remain in ignored `.config`.

## Migration map — 2026-10-09

The migration is being applied in separate commits. The destination paths below define the supported layout after completion; no old-path wrappers are retained.

| Previous location | Destination |
|---|---|
| `examples/` | `use_cases/` (same internal hierarchy) |
| `scripts/sionna_rt/*.py`, `requirements.txt` | `apps/sionna_bridge/` |
| `scripts/sionna_rt/*.sh` | `scripts/local/` |
| `scripts/web_ui/` | `apps/dashboard/` |
| `scripts/native/patches/oai-*`, OAI patch lock | `integrations/oai/` |
| `scripts/native/patches/srsue-*`, srsUE patch lock | `integrations/srsran/` |
| Native OCUDU patches/patch lock; CUDA patches/source locks | `integrations/ocudu/` |
| `tools/usrp-zmq-bridge/` | `integrations/usrp/` |
| `tools/cmx-loop/`, `tools/README.md` | `use_cases/cmx500/` |
| `tests/test_*` | `tests/{core,sionna_bridge,dashboard,integrations,use_cases}/` |
| `tests/bench_mutation_cost.cu` | `benchmarks/bench_mutation_cost.cu` |

Shared workspace locks and platform profiles stay with the scripts that assemble multiple stacks. Patch bytes and source revisions remain unchanged. Repository paths change; executable names, flags, configuration schemas and wire protocols remain stable.

## Supported entrypoints

Run from the repository root unless an absolute path is supplied:

```sh
cmake -S . -B build -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON
cmake --build build
./build/ocudu-gpu-channel --config use_cases/configs/topologies/basic/topology.mvp.cuda.yaml

# Install only the runtime dependencies required by the selected component.
python3 -m pip install '.[dashboard]'
ocudu-dashboard --help
python3 -m pip install '.[sionna]'
ocudu-sionna-bridge --help

bash scripts/local/run_web_ui.sh --help
bash use_cases/robot_fight/run-ocudu-robot-fight.sh --help
bash use_cases/scheduler_benchmark/run-ocudu-scheduler-benchmark.sh --help
```

Direct source entrypoints remain available at `apps/sionna_bridge/run_bridge.py` and `apps/dashboard/server.py`. Installed commands accept the same flags. Supply scenario/scene paths explicitly when using the installed bridge outside the checkout; use-case assets are repository inputs, not bundled package data.

Validation runs on the RTX workstation: CTest for CPU and CUDA builds, Python tests, dashboard JavaScript checks and `scripts/remote/gpu-test-sequence.sh`. The dashboard package includes its HTML and vendored browser modules and does not require Sionna.

## Remote Workspace

The remote workspace is rooted at `REMOTE_WORKSPACE` from `.config`.

```text
~/ocudu-gpu-channel-workspace/
├── ocudu-gpu-channel/
├── ocudu/
├── builds/
│   ├── ocudu-gpu-channel/
│   │   ├── cpu-release/
│   │   └── cuda-release/
│   └── ocudu/
├── configs/
│   ├── local/
│   ├── ocudu/
│   └── distributed/
├── results/
│   ├── benchmarks/
│   ├── logs/
│   ├── pcaps/
│   └── reports/
├── datasets/
├── tools/
│   └── env.sh
└── tmp/
```

`ocudu-gpu-channel/` is a normal Git clone. `ocudu/` is an external dependency checkout and is not copied into this repo. Build outputs and benchmark artifacts stay in remote workspace directories unless summarized results are intentionally promoted into `docs/`.
`tools/` holds user-space CMake, CUDA, and optional ZeroMQ installs; `tools/env.sh` is generated on the remote host and is not tracked.

## Remote Helpers

The scripts in `scripts/remote/` source ignored `.config` and never store private workstation values in tracked files. See `scripts/remote/README.md` for the script matrix; full inventory:

```sh
# Workspace + toolchain
scripts/remote/common.sh                      # shared sourcing (.config + helpers)
scripts/remote/init-workspace.sh              # initialise remote workspace dirs
scripts/remote/bootstrap-user-tools.sh        # user-space CMake/CUDA/ZeroMQ
scripts/remote/probe.sh                       # toolchain sanity check
scripts/remote/sync.sh                        # rsync local tree to remote

# Build + run
scripts/remote/build-and-bench-cuda-mvp.sh    # build + run the CUDA MVP benchmark
scripts/remote/gpu-test-sequence.sh           # nine-stage GPU validation

# OCUDU + srsRAN smokes
scripts/remote/ocudu-attach-smoke.sh          # Milestone A (1 gNB + 1 UE attach + ping)
scripts/remote/ocudu-multi-ue-smoke.sh        # Milestone B (1 gNB + 2 UEs)
scripts/remote/ocudu-multi-gnb-smoke.sh       # Milestone C (2 gNBs + 2 UEs with ICI)
scripts/remote/ocudu-interop-smoke.sh         # broader interop smoke

# Perf sweeps (see scripts/remote/README.md for which to use when)
scripts/remote/perf-sweep.sh                  # CPU + CUDA across every example
scripts/remote/perf-fanin-sweep.sh            # CUDA, 21 generated one-to-N configs
scripts/remote/perf-backend-compare.sh        # CPU vs CUDA matching + speedup
scripts/remote/perf-deep-profile.sh           # Nsight Systems / Compute deep dive
```

Use Wi-Fi only for SSH/control unless a later validation run explicitly proves a wired low-latency data path. Distributed IQ transport requires the network criteria in `docs/distributed.md`.
