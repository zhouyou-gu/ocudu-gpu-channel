# Project Structure

The local repository is the canonical Git source. The remote RTX workstation is a reproducible validation mirror for GPU builds, OCUDU integration, and benchmark runs.

## Local Repository

Tracked project content belongs in:

- `apps/` for executable entrypoints.
- `include/` for public C++ headers.
- `src/` for implementation.
- `tests/` for unit and integration tests.
- `examples/` for small reproducible config examples.
- `docs/` for the technical reference (`ocudu-gpu-channel-doc.html`), OCUDU interop runbook, and distributed-IQ network notes.
- `scripts/` for reproducible local and remote workflows.
- `cmake/` for CMake helper modules when needed.
- `tools/` for developer utilities that are not shipped as main CLIs.

Local-only content stays ignored:

- `.config`
- `build/`, `build-*`, `out/`, `cmake-build-*`
- `results/`, `artifacts/`, `datasets/`, `tmp/`
- `*.log`, `*.pcap`, `*.pcapng`
- local external checkouts such as `ocudu/`

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

The scripts in `scripts/remote/` source ignored `.config` and never store private workstation values in tracked files.

```sh
scripts/remote/init-workspace.sh
scripts/remote/bootstrap-user-tools.sh
scripts/remote/build-and-bench-cuda-mvp.sh
scripts/remote/probe.sh
scripts/remote/sync.sh
```

Use Wi-Fi only for SSH/control unless a later validation run explicitly proves a wired low-latency data path. Distributed IQ transport requires the network criteria in `docs/distributed.md`.
