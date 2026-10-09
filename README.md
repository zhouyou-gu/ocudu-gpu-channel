# ocudu-gpu-channel

Project lead: **[Zhouyou Gu](https://github.com/zhouyou-gu)** (SUTD)<br>
Steering: **[Jihong Park](https://www.sutd.edu.sg/profile/park-jihong/)** (SUTD)<br>
Contributors: **[Minwoo Eun](https://github.com/MinwooEun)** (Yonsei University) · **[Hyunsoo Lee](https://github.com/ehs0)** (Yonsei University) ([contributions](#contributors))

**GPU-accelerated, ZMQ-native channel emulator for live srsRAN and OCUDU stacks.**
Routes `cf32` IQ between radio endpoints and applies CUDA channel models
across multi-gNB, multi-UE and multi-antenna topologies. Processing targets
5G NR slot timing; deadline compliance depends on the workload.

See the [technical reference](https://zhouyou-gu.github.io/ocudu-gpu-channel/)
([local copy](docs/index.html)) for architecture, channel processing and
measured performance.

## Status

- **Available:** CUDA channel models, live Sionna RT matrix updates, a moving
  SUTD scene and one dashboard with independent metrics for each gNB.
- **Validated configurations:** single-UE, multi-UE and two-gNB setups, plus
  rank-1 MISO/SIMO with two or four gNB antenna ports and one port per UE.
  These results apply to the configurations and revisions in the linked reports.
- **Remaining limits:** continuous two-UE moving traffic and strict zero-miss
  real-time qualification still fail. Geometry changes can reset signal history.

See the [integration and validation guide](docs/sionna-integration.md) and
[rank-1 results](docs/rank1-feasibility-report.en.html) for evidence and limits.

## Where this fits

Comparison with existing channel emulators and related simulation tools:

| Tool | Category | Stack | Channel models | In-loop with live radio stacks? |
|---|---|---|---|---|
| **ocudu-gpu-channel** | GPU IQ channel emulator | C++ / CUDA + ZMQ; Python Sionna bridge | TDL-A..E, path-loss, phase, CFO, AWGN, Jakes/Rician fading, and live Sionna matrix profiles | **Yes** — OCUDU / srsRAN via ZMQ; deadline qualification is workload-specific |
| [ACHEM](https://arxiv.org/abs/2604.04742) (arXiv 2026) | Software (CPU) channel emulator / digital twin | Software, USRP-oriented | I/Q-level multipath, mobility, antenna patterns | Yes — validated with GNU Radio, srsRAN 4G/5G, OAI; CPU, scenario replay (no GPU/FPGA) |
| [Colosseum / MCHEM](https://arxiv.org/abs/2110.10617) (MobiCom 2021) | FPGA hardware-in-the-loop emulator | 256 USRP SDRs + FPGA | FIR-tap fading / multipath, up to 256×256 channels | Yes — hardware-in-the-loop, full stacks; shared testbed, not a drop-in box |
| [OpenAirLink](https://arxiv.org/abs/2404.09660) (arXiv 2024) | SDR/FPGA channel emulator | SDR + FPGA | Path-loss + propagation delay via FIR | Reproducible SDR-to-SDR emulation; FPGA-bound |
| [OAI rfsimulator](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/radio/rfsimulator/README.md) | In-loop CPU simulator | C | AWGN + OAI Raytracing Channel Emulator | Yes — only inside the OAI 5G stack, CPU-bound |
| [GNU Radio](https://www.gnuradio.org/) | SDR flowgraph toolkit | C++ / Python | Composable `channels.*` blocks (DIY) | Yes — bring your own SDR or virtual sink |
| [Keysight PROPSIM](https://www.keysight.com/us/en/products/channel-emulators/propsim-platforms.html) / [Spirent Vertex](https://www.spirent.com/products/vertex-channel-emulator) | Commercial RF hardware emulator | Proprietary firmware | 3GPP CDL/TDL, MIMO, full fading at RF | Yes — RF↔RF, commercial pricing |
| [5G-LENA](https://5g-lena.cttc.es/) (ns-3) / [Simu5G](https://github.com/Unipisa/Simu5G) (OMNeT++) | System-level simulator | C++ / Python | TR 38.901 statistical, packet-level | Limited — real-time emulation modes exist but not slot-paced IQ |
| [Sionna](https://github.com/NVlabs/sionna) (NVIDIA) | Link-level simulation and ray tracing | Python; Sionna RT | Channel impulse responses from scene geometry and antenna arrays | This project’s bridge connects Sionna RT outputs to the live IQ channel |
| [MATLAB 5G Toolbox](https://www.mathworks.com/products/5g.html) | Offline link-level simulator | MATLAB | 3GPP CDL/TDL/NTN/HST, MIMO, beamforming | No — commercial license |
| [QuaDRiGa](https://quadriga-channel-model.de/) (Fraunhofer HHI) | Offline channel-impulse-response generator | MATLAB / Octave | 3GPP CDL/TDL, dual-mobility, satellite / NTN, industrial | No |
| [Remcom Wireless InSite](https://www.remcom.com/wireless-insite-em-propagation-software) | Offline 3D ray tracer | Proprietary | Site-specific CIR from 3D scene geometry, mmWave | No — commercial license |

srsRAN's ZMQ driver transports IQ; this emulator adds channel impairments
between the radio endpoints.

## What's inside

- **Implementation:** C++20, CUDA, CMake and libzmq.
- **`ocudu-gpu-channel`** — the broker CLI; sits between two ZMQ endpoints and
  serves processed IQ at slot cadence.
- **`ocudu-gpu-channel-bench`** — per-topology latency benchmark (H2D / kernel /
  D2H, CPU stage timings).
- **`ocudu-zmq-source` / `ocudu-zmq-sink`** — synthetic IQ tools for
  hardware-free validation.
- **CUDA backend** — applies each incoming channel and combines its signal
  into the receiver output.
- **CPU reference backend** — same step set, used by tests and local development.
- **Sionna RT bridge and Web UI** — live matrix updates, moving scene and ray
  views, channel/resource plots, and independent scheduler metrics for each gNB.
- Example topologies in [`use_cases/`](use_cases/): single-edge MVP, 3-node
  interference + crosstalk graph, 2-cell / 4-node multi-gNB, multi-UE OCUDU
  Docker, 16-edge stress, and TR 38.901 §7.7.2 TDL-A through TDL-E profiles.

**Channel models:** TDL-A through TDL-E, path loss, phase, CFO and AWGN,
including multipath with integer/fractional delays, Jakes fading and Rician LOS.
Matrix channels support fixed coefficients, independent fading lanes and spatial
correlation. CPU/CUDA comparisons use each test's stated numerical tolerance.
Full statistical CDL remains [planned](docs/index.html#scope).

**Live matrix updates:** identical and gain/phase-only updates preserve signal
history. Delay/layout changes reset history and start warmup. Control delays
are limited to **1023 samples**; dynamic CUDA matrices require the device-channel
route. Geometry changes are not seamless.

**Radio limits:** live srsUE validation is rank-1 MISO/SIMO. Its single antenna
port does not establish rank-2 operation or same-PRB MU-MIMO. The tested live
downlink uses one radiating gNB branch; see the
[multi-port reference](docs/index.html#part-vii) for the matrix model and evidence.

## Quick start — local synthetic loop

Open four terminals: two synthetic IQ sources, the broker, two paced sinks.

```sh
# Terminal 1: source for TX endpoint 1
./build/ocudu-zmq-source --endpoint tcp://*:2000 --duration 20s

# Terminal 2: source for TX endpoint 2
./build/ocudu-zmq-source --endpoint tcp://*:2101 --duration 20s

# Terminal 3: broker — applies the channel per slot (CPU backend in this local example)
./build/ocudu-gpu-channel --config use_cases/configs/topologies/basic/topology.local.cpu.yaml --duration 20s

# Terminal 4: paced sinks (one request per 1 ms at 23.04 MS/s)
./build/ocudu-zmq-sink --endpoint tcp://127.0.0.1:2001 --duration 10s --request-interval-us 1000
./build/ocudu-zmq-sink --endpoint tcp://127.0.0.1:2100 --duration 10s --request-interval-us 1000
```

`--request-interval-us 1000` is the strict-realtime pace. Drop it for a free-running test.

## Build

Ubuntu 22.04+:

```sh
sudo apt-get install -y cmake g++ pkg-config libzmq3-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure
```

macOS (Homebrew):

```sh
brew install cmake zeromq pkg-config
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

The **CUDA backend builds automatically when `nvcc` is on PATH** at configure
time. Without CUDA, only the CPU backend is built and a `backend: cuda` config
is rejected at load.

## Run in Docker

The repo ships a multi-stage [`Dockerfile`](Dockerfile) that builds the broker
and bakes in the example topologies. Requires the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host for `--gpus all`.

```sh
# Build the GPU image (multi-arch by default: Ampere -> Blackwell + PTX).
docker build -t ocudu-gpu-channel:latest .

# Run the broker on a baked-in example (Linux host networking).
docker run --rm --gpus all --network host ocudu-gpu-channel:latest \
  --config /opt/ocudu/use_cases/configs/topologies/basic/topology.mvp.cuda.yaml --duration 15s
```

Tune for your hardware and host:

```sh
# Faster build for one known GPU (e.g. H100 = sm_90):
docker build --build-arg CUDA_ARCH=90-real -t ocudu-gpu-channel:h100 .

# CPU-only image — no NVIDIA GPU, CI, Mac, or AMD:
docker build \
  --build-arg ENABLE_CUDA=OFF \
  --build-arg DEVEL_BASE=ubuntu:24.04 \
  --build-arg RUNTIME_BASE=ubuntu:24.04 \
  -t ocudu-gpu-channel:cpu .
```

`CUDA_VER` (default `12.8.1`) selects the CUDA base image — lower it to match
an older host driver, dropping `120-real` from `CUDA_ARCH` since Blackwell
needs CUDA ≥ 12.8. The container runs as a non-root `ocudu` user.

On non-host networking (Docker Desktop), publish the control plane instead:
`-p 5559:5559 -p 5560:5560` plus your per-node data endpoints. For real-time
runs, prefer `--cpuset-cpus` pinning over a `--cpus` quota (a CFS quota can
inject scheduling stalls). The deeper [technical reference
§21.2](docs/index.html#container) covers the run contract in full.

## Benchmark

```sh
# CPU reference (any platform)
./build/ocudu-gpu-channel-bench --config use_cases/configs/topologies/basic/topology.local.cpu.yaml --duration 10s --scs-khz 30

# CUDA backend (adds per-stage GPU timings)
./build/ocudu-gpu-channel-bench --config use_cases/configs/topologies/basic/topology.mvp.cuda.yaml --duration 10s --scs-khz 30
```

CUDA output emits `model_mix_latency` plus `h2d_us`, `kernel_us`, `d2h_us`,
`gpu_process_us`, the host-side `host_prep_us` / `host_out_us`, the whole-call
`call_us`, and `cuda_zero_copy` (the host-memory mode that actually ran).
`--per-node` adds the same phases per destination node, which is where a
many-UE gNB shows up. The per-slot gate (green / yellow / red), the methodology,
and the measured fan-in scaling live in
[technical reference §20](docs/index.html#perf).

### Integrated GPUs (DGX Spark GB10, Jetson)

`runtime.cuda_host_memory` selects how the CUDA backend moves per-slot IQ:

| value | behavior |
|---|---|
| `auto` (default) | `zero_copy` when the device reports `cudaDevAttrIntegrated`, else `copy` |
| `copy` | pinned staging buffers + explicit H2D/D2H copies; right for a discrete GPU |
| `zero_copy` | kernels read the input and write the output in host memory directly, and on a device with pageable memory access read the caller's input spans and write its output row in place, so the per-slot IQ copies and the host packing copy disappear |

On GB10 this cuts the emulator call from 46.6 to 25.9 us for the 2-edge MVP,
and for a gNB receiving 64 UEs from 934 to 177 us. Jetson Orin has no pageable
memory access, so only the input and output buffers are mapped; the MVP call
still drops from 251 to 183 us and the live broker p50 by about 20%. On a
discrete GPU `zero_copy` is much slower (every access crosses PCIe), which is
why `auto` keeps it on `copy`. Evidence and the per-buffer breakdown:
[`ZERO_COPY_MILESTONES.md`](ZERO_COPY_MILESTONES.md).

Strict-realtime validation (fails the process on any flow / starvation /
continuity error):

```sh
./build/ocudu-gpu-channel --config use_cases/configs/topologies/basic/topology.mvp.cuda.yaml --duration 20s --strict-realtime
```

## Remote RTX workstation

Validation has three layers: CTest unit tests, the nine-stage synthetic GPU
suite, and live-radio integration tests with real gNB, srsUE and core processes.
The GPU toolchain can be installed in user space:

```sh
./scripts/remote/bootstrap-user-tools.sh        # CMake + CUDA 12.8.1 + ZeroMQ under ~/ocudu-gpu-channel-workspace/tools/
./scripts/remote/probe.sh                       # sanity-check the toolchain
./scripts/remote/build-and-bench-cuda-mvp.sh    # rsync, build, run the CUDA MVP benchmark
./scripts/remote/gpu-test-sequence.sh           # 9 stages: build, CTest, clean/AWGN, interference, multi-gNB, TDL-A, correlated MIMO, live correlation control
```

`gpu-test-sequence.sh` must pass before broker or CUDA backend changes ship.
Recorded RTX 5090 results are tied to their tested revisions:

- **Latest launcher/dashboard checks (`e59e095`):** 90 Python tests and both
  frontend regressions passed; see the [merge report](docs/main-merge-validation.md).
- **Earlier channel checks (`16af288`):** CTest 12/12, all nine GPU stages and
  Compute Sanitizer checks passed; see the
  [channel validation report](docs/sionna-merge-fixes-validation.md).
- **Separate UE recovery (`daa167ae3`):** 18 focused tests passed; an existing
  full NR RRC fixture failure remains documented in the
  [UE validation summary](docs/sionna-integration.md#ue-recovery-validation).

These checks do not establish continuous moving connectivity or zero-miss
real-time operation.

## OCUDU + srsRAN interop

Full OCUDU Docker gNB ↔ broker ↔ srsUE runbook with attach + ping verification:
[**docs/ocudu-interop.md**](docs/ocudu-interop.md).

The default UE repository is
[`zhouyou-gu/srsRAN_4G`](https://github.com/zhouyou-gu/srsRAN_4G). The latest
recovery validation used commit `daa167ae3443b046ce560df646c7dc5f17e5c1dd`
on `fix/sa-ra-recovery-5090`, version `25.10.0`. These local fixes are not
claimed to be on remote `master`. The [integration guide](docs/sionna-integration.md#ue-recovery-validation)
records the exact build, real two-gNB/two-UE results and remaining limits.

End-to-end-validated topologies:

- Single-cell, single-UE: [`use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml`](use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml)
- Multi-UE, one cell, realistic per-UE channel: [`use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml`](use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml)
- 3-node shared-carrier interference graph (UE->UE edges are physical only because all ports share one carrier): [`use_cases/configs/topologies/basic/topology.graph.cuda.yaml`](use_cases/configs/topologies/basic/topology.graph.cuda.yaml)
- 2-cell / 4-node / 8-edge multi-gNB: [`use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml`](use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml)

Synthetic-loop validation matches the analytic superposition to < 0.3 % on real
GPU runs, with all broker data-integrity counters at zero.

### Interference between UEs, TDD cells and real time under load

A port is one carrier's baseband and the broker sums whatever the graph
declares, so an edge must correspond to a physical path. Ports now carry a
carrier label (`tx_carrier` / `rx_carrier`, or `carrier` for TDD) and an edge
between different carriers is rejected before the run; the FDD example
topologies no longer declare UE<->UE edges. Each radio's software transmit
level is put on one scale with a per-device `tx_scale_db`. On a TDD n78 cell
with two OAI nrUEs, and on two cells with different TDD patterns, the other
UE's uplink lands in the victim's downlink slots through a real Sionna RT
path, and the stack shows it as CQI collapse, DL NACKs and RTT spikes
(`CROSSTALK_MILESTONES.md`, `docs/plans/x3-tdd-multi-ue.md`,
`docs/plans/x5-tdd-cli.md`). The same work fixed a broker bug (a `phase` step
applied the link CFO a second time) and added a relay wedge detector
(`OCG_BROKER_WEDGE_TIMEOUT_MS`).

Over ZMQ, OCUDU runs its lower PHY sequentially, which caps the lock-step loop.
`scripts/native/build-ocudu-gnb-local.sh` builds the pinned gNB with local
patches that keep a threaded lower PHY, give the radio worker real-time
priority and drop the double copy; on the GB10 the 100 MHz 2x2 idle real-time
factor goes from 0.65 to 0.82 and 20 MHz under load reaches 0.98
(`scripts/native/README.md`, `SPARK_MILESTONES.md` S17).

## Deeper docs

- [Current Sionna integration](docs/sionna-integration.md) — contributor
  provenance, channel updates, one multi-gNB dashboard, UE recovery and
  qualification limits.
- [Technical reference](docs/index.html) — architecture,
  topology and YAML model, broker per-slot loop, signal alignment, GPU compute,
  signal memory, multi-stream concurrency, profiling, performance, planned
  work. **Start here for design questions.**
- [Platforms](docs/platforms.md) — RTX 5090, DGX Spark GB10 and Jetson AGX
  Orin: what differs per host (host memory mode, CPU placement, CUDA gNB locks
  and patches, local stack patches), measured envelopes, and how each host is
  set up.
- [OCUDU interop runbook](docs/ocudu-interop.md) — Docker gNB + srsUE attach
  procedure.
- [Distributed IQ over network](docs/distributed.md) — bandwidth, jitter, and
  packet-loss requirements when broker and radios run on different hosts.
- [Project structure](docs/project-structure.md) — local repo and remote RTX
  workstation layout.

## Contributors

This table is the project's authoritative contributor list. Original commits
and authorship are retained; contribution areas can overlap.

| Contributor | Affiliation | Role | Contribution areas |
|---|---|---|---|
| **[Zhouyou Gu](https://github.com/zhouyou-gu)** | Singapore University of Technology and Design (SUTD) | Project lead | Core broker and CPU/CUDA emulator, topology and runtime control, OCUDU/srsRAN interop, integration fixes and RTX validation, and multi-UE attachment/recovery work in the user-owned srsRAN fork. |
| **[Jihong Park](https://www.sutd.edu.sg/profile/park-jihong/)** | Singapore University of Technology and Design (SUTD) | Steering | Project steering. |
| **[Minwoo Eun](https://github.com/MinwooEun)** | Yonsei University | Rank-1 MISO/SIMO contributor | Multi-port radios and physical-link state, fixed and correlated matrix channels, live 2×1/1×2 and 4×1/1×4 gates, wire-capture scoring and transport validation, and precoding/UE feasibility studies. |
| **[Hyunsoo Lee](https://github.com/ehs0)** | Yonsei University | Sionna integration contributor | Sionna RT bridge and live scalar/matrix updates, native integration, moving SUTD scene and antenna arrays, scene/ray/channel visualization, timing/resource views, and the initial gNB scheduler KPI panel. |

The [integration guide](docs/sionna-integration.md#contribution-provenance)
records the normal merge and distinguishes the original Sionna work from
subsequent channel, dashboard and UE recovery fixes.

## License

Released under the [MIT License](LICENSE). Copyright © 2026 Zhouyou Gu.
