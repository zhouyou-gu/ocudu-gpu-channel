# ocudu-gpu-channel

**GPU-accelerated, ZMQ-native channel emulator for live srsRAN and OCUDU stacks.**
Drops between two ZMQ radios, routes `cf32` IQ across multi-gNB / multi-UE
topologies, and applies CUDA channel models inside a hard 1&nbsp;ms slot deadline.

This is the project landing page. For architecture, broker internals, GPU
kernel design, profiling, and performance numbers, see the
[**technical reference**](docs/ocudu-gpu-channel-doc.html).

## Status

What's proven end-to-end on an RTX 5090 against the OCUDU + srsUE stack:

- **Milestone A — single UE attach.** OCUDU gNB ↔ CUDA broker ↔ srsUE:
  `rrc_connected=1`, `pdu_session_established=1`, IP ping OK; broker
  data-integrity counters all zero; 0 gNB `Real-time failure in RF: overflow`.
- **Milestone B — multi-UE on one cell.** Two srsUEs through one gNB over a
  realistic per-UE channel (per-link path-loss + phase + AWGN); both attached
  with distinct C-RNTIs and PDU sessions.
- **Milestone C — multi-gNB with interference.** Two OCUDU gNBs + two srsUEs
  (one per cell) on a 4-node / 8-link inter-cell-interference topology; each
  gNB's RX is the GPU superposition of its serving UE plus the other cell's
  interferer; both UEs attach to their own cell.

**Supported chain steps today:** `tdl` (tapped delay line — covers
scalar gain, integer or fractional sample delay, full multi-tap multipath,
and per-tap Doppler-shaped fading with optional Rician LOS specular via the
same step), `path_loss`, `phase`, `cfo`, `awgn` — CUDA and CPU, bit-exact.
The 3GPP TR 38.901 §7.7.2 TDL-A through TDL-E profiles ship as
[`examples/topology.tdl-{a..e}.cuda.yaml`](examples/).

**Next:** full CDL (TR 38.901 §7.7.1) with per-cluster angles, polarisation,
and antenna array response, once a real MIMO / beamforming use case
surfaces. See
[technical reference §19](docs/ocudu-gpu-channel-doc.html#scope) for the
architecture and decisions.

## Where this fits

Adjacent tools cover offline link-level simulation, offline channel-impulse-response generation, offline 3D ray-tracing, system-level discrete-event simulation, SDR flowgraph toolkits, in-loop CPU simulators tied to specific 5G stacks, and commercial RF↔RF hardware emulators. ocudu-gpu-channel fills the gap they leave — *the GPU-accelerated, ZMQ-native channel emulator for live srsRAN and OCUDU stacks at slot cadence.*

| Tool | Category | Stack | Channel models | In-loop with live radio stacks? |
|---|---|---|---|---|
| **ocudu-gpu-channel** | Real-time GPU emulator | C++ / CUDA + ZMQ | Multi-tap delay, path-loss, phase, CFO, AWGN, Jakes fading + Rician LOS (TR 38.901 §7.7.2 TDL-A..E) | **Yes** — OCUDU / srsRAN via ZMQ on a 1 ms slot budget |
| [OAI rfsimulator](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/radio/rfsimulator/README.md) | In-loop CPU simulator | C | AWGN + OAI Raytracing Channel Emulator | Yes — only inside the OAI 5G stack, CPU-bound |
| [GNU Radio](https://www.gnuradio.org/) | SDR flowgraph toolkit | C++ / Python | Composable `channels.*` blocks (DIY) | Yes — bring your own SDR or virtual sink |
| [Keysight PROPSIM](https://www.keysight.com/us/en/products/channel-emulators/propsim-platforms.html) / [Spirent Vertex](https://www.spirent.com/products/vertex-channel-emulator) | Commercial RF hardware emulator | Proprietary firmware | 3GPP CDL/TDL, MIMO, full fading at RF | Yes — RF↔RF, commercial pricing |
| [5G-LENA](https://5g-lena.cttc.es/) (ns-3) / [Simu5G](https://github.com/Unipisa/Simu5G) (OMNeT++) | System-level simulator | C++ / Python | TR 38.901 statistical, packet-level | Limited — real-time emulation modes exist but not slot-paced IQ |
| [Sionna](https://github.com/NVlabs/sionna) (NVIDIA) | Offline GPU link-level + ray tracing | Python + TensorFlow / Keras | 3GPP CDL/TDL, MIMO / OFDM, differentiable ray tracing (Sionna RT) | No — research / ML training in notebooks |
| [MATLAB 5G Toolbox](https://www.mathworks.com/products/5g.html) | Offline link-level simulator | MATLAB | 3GPP CDL/TDL/NTN/HST, MIMO, beamforming | No — commercial license |
| [QuaDRiGa](https://quadriga-channel-model.de/) (Fraunhofer HHI) | Offline channel-impulse-response generator | MATLAB / Octave | 3GPP CDL/TDL, dual-mobility, satellite / NTN, industrial | No |
| [Remcom Wireless InSite](https://www.remcom.com/wireless-insite-em-propagation-software) | Offline 3D ray tracer | Proprietary | Site-specific CIR from 3D scene geometry, mmWave | No — commercial license |

Note: srsRAN's own ZMQ driver is a raw IQ pipe with no channel impairments — ocudu-gpu-channel is what you drop into that pipe to make it interesting.

## What's inside

- C++20 + CMake project on libzmq.
- **`ocudu-gpu-channel`** — the broker CLI; sits between two ZMQ endpoints and
  serves processed IQ at slot cadence.
- **`ocudu-gpu-channel-bench`** — per-topology latency benchmark (H2D / kernel /
  D2H, CPU stage timings).
- **`ocudu-zmq-source` / `ocudu-zmq-sink`** — synthetic IQ tools for
  hardware-free validation.
- **CUDA backend** — fused `superpose_kernel` that walks every incoming edge of
  a node and accumulates per-edge channel shaping into one RX signal per slot.
- **CPU reference backend** — same step set, used by tests and local development.
- Example topologies in [`examples/`](examples/): single-link MVP, 3-node
  interference + crosstalk graph, 2-cell / 4-node multi-gNB, multi-UE OCUDU
  Docker, 16-edge stress, and TR 38.901 §7.7.2 TDL-A through TDL-E profiles.

## Quick start — local synthetic loop

Open four terminals: two synthetic IQ sources, the broker, two paced sinks.

```sh
# Terminal 1: source for TX endpoint 1
./build/ocudu-zmq-source --endpoint tcp://*:2000 --duration 20s

# Terminal 2: source for TX endpoint 2
./build/ocudu-zmq-source --endpoint tcp://*:2101 --duration 20s

# Terminal 3: broker — runs CUDA channel kernels per slot
./build/ocudu-gpu-channel --config examples/topology.local.yaml --duration 20s

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

## Benchmark

```sh
# CPU reference (any platform)
./build/ocudu-gpu-channel-bench --config examples/topology.local.yaml --duration 10s --scs-khz 30

# CUDA MVP (adds per-stage GPU timings)
./build/ocudu-gpu-channel-bench --config examples/topology.cuda-mvp.yaml --duration 10s --scs-khz 30
```

CUDA output emits `model_mix_latency` plus `h2d_us`, `kernel_us`, `d2h_us`,
`gpu_process_us`. The per-slot gate (green / yellow / red), the methodology,
and the measured fan-in scaling live in
[technical reference §21](docs/ocudu-gpu-channel-doc.html#perf).

Strict-realtime validation (fails the process on any flow / starvation /
continuity error):

```sh
./build/ocudu-gpu-channel --config examples/topology.cuda-mvp.yaml --duration 20s --strict-realtime
```

## Remote RTX workstation

The remote GPU path is user-space only — no root needed:

```sh
./scripts/remote/bootstrap-user-tools.sh        # CMake + CUDA 12.8.1 + ZeroMQ under ~/ocudu-gpu-channel-workspace/tools/
./scripts/remote/probe.sh                       # sanity-check the toolchain
./scripts/remote/build-and-bench-cuda-mvp.sh    # rsync, build, run the CUDA MVP benchmark
./scripts/remote/gpu-test-sequence.sh           # full 7-step validation: build + ctest + clean/AWGN relays + interference graph + 2-cell multi-gNB + TDL-A profile
```

`gpu-test-sequence.sh` is the locked-in GPU validation run; it must pass before
any change to the broker or CUDA backend ships.

## OCUDU + srsRAN interop

Full OCUDU Docker gNB ↔ broker ↔ srsUE runbook with attach + ping verification:
[**docs/ocudu-interop.md**](docs/ocudu-interop.md).

End-to-end-validated topologies:

- Single-cell, single-UE: [`examples/topology.ocudu-docker.cuda.yaml`](examples/topology.ocudu-docker.cuda.yaml)
- Multi-UE, one cell, realistic per-UE channel: [`examples/topology.ocudu-docker.multi-ue.cuda.yaml`](examples/topology.ocudu-docker.multi-ue.cuda.yaml)
- 3-node interference + crosstalk graph: [`examples/topology.graph.cuda.yaml`](examples/topology.graph.cuda.yaml)
- 2-cell / 4-node / 8-link multi-gNB: [`examples/topology.multi-gnb.cuda.yaml`](examples/topology.multi-gnb.cuda.yaml)

Synthetic-loop validation matches the analytic superposition to < 0.3 % on real
GPU runs, with all broker data-integrity counters at zero.

## Deeper docs

- [Technical reference](docs/ocudu-gpu-channel-doc.html) — architecture,
  topology and YAML model, broker per-slot loop, signal alignment, GPU compute,
  signal memory, multi-stream concurrency, profiling, performance, planned
  work. **Start here for design questions.**
- [OCUDU interop runbook](docs/ocudu-interop.md) — Docker gNB + srsUE attach
  procedure.
- [Distributed IQ over network](docs/distributed.md) — bandwidth, jitter, and
  packet-loss requirements when broker and radios run on different hosts.
- [Project structure](docs/project-structure.md) — local repo and remote RTX
  workstation layout.
