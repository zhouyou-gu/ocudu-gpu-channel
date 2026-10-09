# Running the broker

This guide runs the broker as a process or container. Build it using [installation](../getting_started/installation.md), choose a validated topology, and start its matching ZMQ sources and sinks. Run commands from the repository root. The [first-run tutorial](../getting_started/first-run.md) supplies a complete concurrent peer setup, expected output and cleanup.

For an existing set of peers:

```sh
build/ocudu-gpu-channel \
  --config use_cases/configs/topologies/basic/topology.mvp.cuda.yaml \
  --duration 15s
```

Expect broker startup and a final `event=stop` record with nonzero receive requests. A broker with no requesting peers does no channel work. The duration ends the broker; stop independently launched peers through their owning shell or launcher. See [troubleshooting](troubleshooting.md) for socket and starvation failures.

(section-21)=

## Operational guidance

- **Use `--strict-realtime`** for CI and any merge gate — it turns starvation, overflow, and continuity events into hard exit-non-zero failures.
- **Reading the latency gate**: green = p99 added latency ≤ 25 % of the NR slot; yellow = stable, ≤ one slot; red = \> one slot or unstable. A benchmark band does not qualify a live radio run; retain the complete integrity counters and the live gate verdict.
- **Sample rates other than 23.04 MS/s** work as long as the topology declares them consistently. The slot batch defaults to `auto` (= 1 ms worth of samples).
- **If `rx_starvations` climbs** in production: usually the host CPU is slower than the sample-rate cadence on whichever node started missing deadlines. Check the kernel timings first (`event=gpu_timings`); the GPU is rarely the bottleneck.
- **Distributed IQ across machines** over Wi-Fi or VPN is not viable — see [`docs/distributed.md`](distributed.md). Prefer the same host; qualify any wired transport against the measured latency and throughput budget.

(latency-gate)=

### Latency-gate verdict

The bench CLI publishes a green / yellow / red verdict alongside the `p99` latency, defined formally as:

| Band | Condition | Meaning |
|----|----|----|
| **green** | p99 added latency ≤ 25 % of the slot | Plenty of margin — the slot deadline is met even with reasonable jitter. |
| **yellow** | p99 added latency ≤ one slot | Stable but tight — a brief OS scheduling hiccup can blow the deadline. |
| **red** | p99 added latency \> one slot | Exceeds the slot budget in this benchmark; investigate before a live gate. |

The bench's latency is in-memory model mixing only; ZMQ I/O, broker scheduling, and host↔device contention are not in it. The broker itself measures the full pipeline. Both rows are reported in the [performance section](../reports/performance/measured-boundaries.md#perf) so the gap is visible.

(container)=

### Containerised deployment

The repo ships a multi-stage `Dockerfile` at the root that packages the broker and the companion CLIs (`ocudu-gpu-channel-bench`, `ocudu-zmq-source`, `ocudu-zmq-sink`) and bakes the example topologies into the image at `/opt/ocudu/use_cases`. The runtime is well-suited to containerisation: it has no hardcoded host paths and is entirely CLI-driven (`--config`, `--control-endpoint`, `--telemetry-endpoint`, …), so the only host coupling is the GPU and the ZMQ sockets.

**Two-stage image.** A `build` stage on `nvidia/cuda:<ver>-devel` installs `cmake pkg-config libzmq3-dev` and compiles everything (apps and CTest targets); a slim `runtime` stage on `nvidia/cuda:<ver>-runtime` carries only `libzmq5`, the four binaries, and the use-case configurations, running as a non-root `ocudu` user. Prerequisite for GPU access is the NVIDIA Container Toolkit on the host — the image ships the CUDA runtime libraries, the host provides the driver.

| Build arg | Default | Why it exists (portability) |
|----|----|----|
| `CUDA_ARCH` | `80-real;86-real;89-real;90-real;120-real;80-virtual` | Multi-arch by default: native SASS for Ampere → Blackwell plus PTX from compute_80, so any GPU ≥ 8.0 not in the list JIT-compiles at first launch. Override to a single arch (e.g. `90-real`) for a smaller, faster build. |
| `CUDA_VER` | `12.8.1` | Selects the CUDA base image. The host driver must support this runtime version; lower it to match an older driver, and drop `120-real` since Blackwell needs CUDA ≥ 12.8. |
| `ENABLE_CUDA` | `ON` | Set `OFF` (with `DEVEL_BASE` / `RUNTIME_BASE` pointed at `ubuntu:24.04`) for a GPU-less image on hosts without an NVIDIA GPU — CI, macOS, AMD, laptops. The CPU backend is always built. |

**Run contract.** On Linux, host networking exposes the topology’s loopback endpoints to host peers. Start the matching peers before this broker command:

    docker build -t ocudu-gpu-channel:latest .
    docker run --rm --gpus all --network host ocudu-gpu-channel:latest \
      --config /opt/ocudu/use_cases/configs/topologies/basic/topology.mvp.cuda.yaml --duration 15s

On non-host networking (Docker Desktop), publish the control plane with `-p 5559:5559 -p 5560:5560` plus the per-node data endpoints instead. **Real-time caveat:** prefer `--cpuset-cpus` CPU pinning over a `--cpus` quota — a CFS quota can inject scheduling stalls the broker surfaces as `rx_starvations`.

**Testing in the container.** Because `docker build` has no GPU access, the CUDA-gated tests (`hardware_probe`, `runtime_update_parity`, `broker`) cannot run in a build-time layer; they run post-build against the `build` target with a GPU attached:

    docker build --target build -t ocudu-gpu-channel:build .
    docker run --rm --gpus all --entrypoint bash ocudu-gpu-channel:build \
      -c 'cd /src/build && ctest --output-on-failure'

The multi-arch list is compiled for every target but only arch `120` (the RTX 5090) is hardware-validated; the other arches are built but unverified for lack of the hardware.
