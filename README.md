# ocudu-gpu-channel

`ocudu-gpu-channel` is a GPU-targeted standalone ZMQ IQ channel emulator for OCUDU Split 8 SDR workflows. It is designed to sit between OCUDU-compatible ZMQ endpoints, route `cf32` IQ streams across multi-gNB and multi-UE topologies, and apply deterministic channel models on CUDA while measuring the added real-time latency.

The project target is real-time GPU channel emulation. CPU code exists only as a correctness reference, local development path, and baseline for proving when CUDA helps.

The current implementation is an initial GPU-first community scaffold:

- C++20/CMake project with libzmq.
- Standalone broker CLI: `ocudu-gpu-channel`.
- Benchmark CLI: `ocudu-gpu-channel-bench`.
- Synthetic OCUDU-style ZMQ source/sink tools.
- CUDA MVP backend for gain, path loss, fixed phase, and CFO. AWGN and delay models are rejected on CUDA until kernels exist.
- Explicit CPU reference backend for local tests, CPU/GPU comparison, and models not yet ported to CUDA.

## Build

Ubuntu 22.04+:

```sh
sudo apt-get update
sudo apt-get install -y cmake g++ pkg-config libzmq3-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure
```

macOS with Homebrew:

```sh
brew install cmake zeromq pkg-config
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

## Run a Local Synthetic Loop

Terminal 1:

```sh
./build/ocudu-zmq-source --endpoint tcp://*:2000 --duration 20s
```

Terminal 2:

```sh
./build/ocudu-zmq-source --endpoint tcp://*:2101 --duration 20s
```

Terminal 3:

```sh
./build/ocudu-gpu-channel --config examples/topology.local.yaml --duration 20s
```

Terminal 4:

```sh
./build/ocudu-zmq-sink --endpoint tcp://127.0.0.1:2001 --duration 10s
./build/ocudu-zmq-sink --endpoint tcp://127.0.0.1:2100 --duration 10s
```

For strict realtime validation, pace sink requests to the configured batch duration. With `batch_samples: auto` at 23.04 MS/s, that is one request per 1 ms:

```sh
./build/ocudu-zmq-sink --endpoint tcp://127.0.0.1:2001 --duration 10s --request-interval-us 1000
```

## Model Benchmark

```sh
./build/ocudu-gpu-channel-bench --config examples/topology.local.yaml --duration 10s --scs-khz 30
```

The CPU topology is a baseline/reference run. The project-facing benchmark target is the CUDA MVP topology below. The benchmark reports latency percentiles for in-memory model mixing only. It does not include ZMQ receive/send time or full broker scheduling overhead. The gate is intentionally conservative and is useful for model-path triage before full interop benchmarking:

- green: p99 added processing latency is at most 25% of the configured NR slot duration;
- yellow: stable but at most one slot;
- red: more than one slot or unstable.

On a CUDA build, use the MVP topology to collect the GPU transfer and kernel timing rows:

```sh
./build/ocudu-gpu-channel-bench --config examples/topology.cuda-mvp.yaml --duration 10s --scs-khz 30
```

CUDA output keeps `model_mix_latency` and adds `h2d_us`, `kernel_us`, `d2h_us`, and `gpu_process_us`.

For validation runs where zero flow, starvation, or continuity failures should fail the process:

```sh
./build/ocudu-gpu-channel --config examples/topology.cuda-mvp.yaml --duration 20s --strict-realtime
```

## Remote RTX Workstation

The remote GPU path is intentionally user-space only:

```sh
./scripts/remote/bootstrap-user-tools.sh
./scripts/remote/probe.sh
./scripts/remote/build-and-bench-cuda-mvp.sh
```

The bootstrap installs CMake, CUDA Toolkit 12.8.1, and ZeroMQ if needed under `~/ocudu-gpu-channel-workspace/tools/`, then writes `tools/env.sh` on the remote host. Source that file before remote CMake builds.

For OCUDU runtime interop, use the Docker gNB runbook and smoke helpers in [docs/ocudu-interop.md](docs/ocudu-interop.md). The first milestone proves OCUDU ZMQ sample flow through the CUDA broker; the second attempts srsUE attach and ping through the same broker path.

## Known Limits

- CUDA is the target backend. CPU mode is a reference/baseline path and should not be used for GPU scale claims.
- The CUDA MVP currently covers gain, path loss, phase, and CFO; AWGN and delay models are intentionally rejected on CUDA until kernels exist.
- The broker is a standalone external process, not an OCUDU patch.
- Scale is reported as a measured envelope by topology, sample rate, model chain, CPU affinity, and backend. The project does not promise fixed multi-UE or multi-gNB counts before benchmark data exists.
- Distributed IQ over Wi-Fi or VPN is not considered viable. See [docs/distributed.md](docs/distributed.md).
