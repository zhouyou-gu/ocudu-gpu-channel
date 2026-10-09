# Remote workspace

The local repository is canonical; the RTX host is its validation mirror. Load ignored credentials only through Bash remote helpers, and keep builds and evidence outside tracked source.

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

The scripts in `scripts/remote/` source ignored `.config` and never store private workstation values in tracked files. See the [remote script index](../../scripts/remote/README.md) for synchronization behavior, shared upload exclusions and the full script matrix. Common helpers:

```sh
# Workspace + toolchain
scripts/remote/common.sh                      # shared sourcing (.config + helpers)
scripts/remote/init-workspace.sh              # initialise remote workspace dirs
scripts/remote/bootstrap-user-tools.sh        # user-space CMake/CUDA/ZeroMQ
scripts/remote/probe.sh                       # toolchain sanity check
scripts/remote/sync.sh                        # fetch + fast-forward remote branch from origin
scripts/remote/bootstrap-sionna.sh            # optional remote Sionna environment

# Build + run
scripts/remote/build-and-bench-cuda-mvp.sh    # build + run the CUDA MVP benchmark
scripts/remote/gpu-test-sequence.sh           # nine-stage GPU validation

# OCUDU + srsRAN smokes
scripts/remote/ocudu-attach-smoke.sh          # Milestone A (1 gNB + 1 UE attach + ping)
scripts/remote/ocudu-multi-ue-smoke.sh        # Milestone B (1 gNB + 2 UEs)
scripts/remote/ocudu-multi-gnb-smoke.sh       # Milestone C (2 gNBs + 2 UEs with ICI)
scripts/remote/ocudu-interop-smoke.sh         # broader interop smoke

# Perf sweeps (see scripts/remote/README.md for which to use when)
scripts/remote/perf-sweep.sh                  # CPU + CUDA configuration sweep
scripts/remote/perf-fanin-sweep.sh            # CUDA, 21 generated one-to-N configs
scripts/remote/perf-backend-compare.sh        # CPU vs CUDA matching + speedup
scripts/remote/perf-deep-profile.sh           # Nsight Systems / Compute deep dive
```

Use Wi-Fi only for SSH/control unless a later validation run explicitly proves a wired low-latency data path. Distributed IQ transport requires the network criteria in [distributed operation](distributed.md).

## Run ownership

Use an isolated validation directory for new checks. Preserve existing services and keep logs, resolved revisions and commands together. See [testing](../development/testing.md) for verdict requirements.
