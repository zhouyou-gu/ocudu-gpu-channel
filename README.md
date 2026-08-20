# ocudu-gpu-channel

Author: **Zhouyou Gu**, SUTD

**GPU-accelerated, ZMQ-native channel emulator for live srsRAN and OCUDU stacks.**
Drops between two ZMQ radios, routes `cf32` IQ across multi-gNB / multi-UE
topologies, and applies CUDA channel models inside the 5G&nbsp;NR slot
deadline (1&nbsp;ms at 15&nbsp;kHz SCS, 500&nbsp;µs at 30&nbsp;kHz SCS — the
bench default). Radios may have several antenna ports: their ports group into
one radio node sharing one sample epoch, and the link between two such radios
carries an `Nt×Nr` matrix channel.

This is the project landing page. For architecture, broker internals, GPU
kernel design, profiling, and performance numbers, see the
[**technical reference**](https://zhouyou-gu.github.io/ocudu-gpu-channel/)
(rendered via GitHub Pages — falls back to a local clone of
[`docs/index.html`](docs/index.html) until Pages is enabled on the repo).

## Status

What's proven end-to-end on an RTX 5090 against the OCUDU + srsUE stack.

**Two naming axes, kept distinct so they don't read as competing.**
*Milestone A/B/C* are the live-radio proof points — what works end-to-end,
validated by the **live-radio integration** smokes (`ocudu-*-smoke.sh`).
*Phase 1/2/3* are the internal build roadmap — how it was built, validated by
the **unit tests** (`ctest`) and the **synthetic GPU validation**
(`gpu-test-sequence.sh`). The three test layers are summarised under
[Remote RTX workstation](#remote-rtx-workstation) below.

- **Milestone A — single UE attach.** OCUDU gNB ↔ CUDA broker ↔ srsUE:
  `rrc_connected=1`, `pdu_session_established=1`, IP ping OK; broker
  data-integrity counters all zero; 0 gNB `Real-time failure in RF: overflow`.
- **Milestone B — multi-UE on one cell.** Four srsUEs through one gNB over a
  realistic per-UE channel (per-edge path-loss + phase + AWGN); all four
  attached with distinct C-RNTIs, PDU sessions and IPs, each on its first
  random-access attempt. Multi-UE attach needs a recent srsUE: on
  `release_23_11` a UE that loses RACH contention reports a successful attach
  and stops retrying, so only one of four ever gets a session. The smokes build
  srsUE from the latest
  [`zhouyou-gu/srsRAN_4G`](https://github.com/zhouyou-gu/srsRAN_4G) `master`,
  which fixes that, plus a `SRSUE_PRACH_PREAMBLE_INDEX` override that pins each
  UE to its own preamble — not required for attach, but it avoids the contention
  altogether so every UE succeeds first try and runs are reproducible.
- **Milestone C — multi-gNB with interference.** Two OCUDU gNBs + two srsUEs
  (one per cell) on a 4-node / 8-edge inter-cell-interference topology; each
  gNB's RX is the GPU superposition of its serving UE plus the other cell's
  interferer; both UEs attach to their own cell.
- **Phase 2 device channel pipeline — TR 38.901 profiles realtime-fit.** The
  per-edge channel (multi-tap convolution + Jakes Doppler + Rician LOS) runs
  on the GPU by default via `apply_channel_kernel`; host `stage_link()` stays
  as the CPU reference and the CUDA fallback. Moving it host → device took the
  per-edge channel for `tdl-a_E16` (1 gNB + 8 UEs, TDL-A 23-tap + Jakes 100 Hz
  on all 16 edges) from **58 430 µs → 319 µs** (≈183×) — well inside the 1 ms
  slot budget.

- **Multi-port radios and the matrix channel.** Ports group under
  `radio_nodes:` (writing order is the matrix index) and a link between two
  radios carries an `Nt×Nr` matrix: deterministic (`fixed_mimo`), stochastic
  with independent lanes, or spatially correlated with a coherent LOS
  component (`spatial_correlation`, `los_matrix`). Live gate: a real
  2-antenna OCUDU gNB over four ZMQ endpoints, 20 s, ~1174 four-endpoint
  groups/s, every strict counter zero, 0 gNB `Real-time failure in RF`, and an
  independent check that recomputes `y = Hx` from the captured wires — max
  `|y − Hx|` of 4.1e−08 (DL) and 1.5e−07 (UL) against a 1e−4 tolerance, with
  12–77% of each row's amplitude coming from the *other* transmit port.
  **This is transport and channel evidence, not a rank-2 claim** — a live
  rank > 1 link needs a UE PHY that jointly decodes a matrix channel, and the
  integrated srsUE reads antenna 0 only. See
  [technical reference Part VII](docs/index.html#part-vii).

**Supported chain steps today:** `tdl` (tapped delay line — covers
scalar gain, integer or fractional sample delay, full multi-tap multipath,
and per-tap Doppler-shaped fading with optional Rician LOS specular via the
same step), `path_loss`, `phase`, `cfo`, `awgn` — CUDA and CPU, bit-exact at
1e-3 tolerance. The 3GPP TR 38.901 §7.7.2 TDL-A through TDL-E profiles ship
as [`examples/topology.tdl-{a..e}.cuda.yaml`](examples/) and all run on the
device kernel by default.

**Next:** full CDL (TR 38.901 §7.7.1) with per-cluster angles, polarisation,
and antenna array response, once a beamforming use case surfaces — the matrix
channel that ships today is declared or drawn, not synthesised from an array
geometry. See
[technical reference §26](docs/index.html#scope) for the
architecture and decisions; the Phase 2 device pipeline plan + measured
record lives in
[`docs/plans/device-channel-pipeline.md`](docs/plans/device-channel-pipeline.md).

## Where this fits

Adjacent tools cover offline link-level simulation, offline channel-impulse-response generation, offline 3D ray-tracing, system-level discrete-event simulation, SDR flowgraph toolkits, in-loop CPU simulators tied to specific 5G stacks, software and FPGA channel emulators for real stacks, and commercial RF↔RF hardware emulators. ocudu-gpu-channel fills the gap they leave — *the GPU-accelerated, ZMQ-native channel emulator for live srsRAN and OCUDU stacks at slot cadence.*

| Tool | Category | Stack | Channel models | In-loop with live radio stacks? |
|---|---|---|---|---|
| **ocudu-gpu-channel** | Real-time GPU emulator | C++ / CUDA + ZMQ | Multi-tap delay, path-loss, phase, CFO, AWGN, Jakes fading + Rician LOS (TR 38.901 §7.7.2 TDL-A..E) — channel runs on the GPU by default | **Yes** — OCUDU / srsRAN via ZMQ on a 1 ms slot budget |
| [ACHEM](https://arxiv.org/abs/2604.04742) (arXiv 2026) | Software (CPU) channel emulator / digital twin | Software, USRP-oriented | I/Q-level multipath, mobility, antenna patterns | Yes — validated with GNU Radio, srsRAN 4G/5G, OAI; CPU, scenario replay (no GPU/FPGA) |
| [Colosseum / MCHEM](https://arxiv.org/abs/2110.10617) (MobiCom 2021) | FPGA hardware-in-the-loop emulator | 256 USRP SDRs + FPGA | FIR-tap fading / multipath, up to 256×256 channels | Yes — hardware-in-the-loop, full stacks; shared testbed, not a drop-in box |
| [OpenAirLink](https://arxiv.org/abs/2404.09660) (arXiv 2024) | SDR/FPGA channel emulator | SDR + FPGA | Path-loss + propagation delay via FIR | Reproducible SDR-to-SDR emulation; FPGA-bound |
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
- Example topologies in [`examples/`](examples/): single-edge MVP, 3-node
  interference + crosstalk graph, 2-cell / 4-node multi-gNB, multi-UE OCUDU
  Docker, 16-edge stress, and TR 38.901 §7.7.2 TDL-A through TDL-E profiles.

## Quick start — local synthetic loop

Open four terminals: two synthetic IQ sources, the broker, two paced sinks.

```sh
# Terminal 1: source for TX endpoint 1
./build/ocudu-zmq-source --endpoint tcp://*:2000 --duration 20s

# Terminal 2: source for TX endpoint 2
./build/ocudu-zmq-source --endpoint tcp://*:2101 --duration 20s

# Terminal 3: broker — applies the channel per slot (CPU backend in this local example)
./build/ocudu-gpu-channel --config examples/topology.local.cpu.yaml --duration 20s

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
  --config /opt/ocudu/examples/topology.mvp.cuda.yaml --duration 15s
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
./build/ocudu-gpu-channel-bench --config examples/topology.local.cpu.yaml --duration 10s --scs-khz 30

# CUDA backend (adds per-stage GPU timings)
./build/ocudu-gpu-channel-bench --config examples/topology.mvp.cuda.yaml --duration 10s --scs-khz 30
```

CUDA output emits `model_mix_latency` plus `h2d_us`, `kernel_us`, `d2h_us`,
`gpu_process_us`. The per-slot gate (green / yellow / red), the methodology,
and the measured fan-in scaling live in
[technical reference §20](docs/index.html#perf).

Strict-realtime validation (fails the process on any flow / starvation /
continuity error):

```sh
./build/ocudu-gpu-channel --config examples/topology.mvp.cuda.yaml --duration 20s --strict-realtime
```

## Remote RTX workstation

The project is validated in **three test layers**: **unit tests** (`ctest`,
hardware-free — parity, control plane, broker), **synthetic GPU validation**
(`gpu-test-sequence.sh`, 7 steps on the RTX 5090 — no live radio), and
**live-radio integration** (`ocudu-*-smoke.sh` — the Milestone A/B/C attaches
through a real srsRAN gNB + srsUE). The remote GPU path is user-space only — no
root needed:

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
- 2-cell / 4-node / 8-edge multi-gNB: [`examples/topology.multi-gnb.cuda.yaml`](examples/topology.multi-gnb.cuda.yaml)

Synthetic-loop validation matches the analytic superposition to < 0.3 % on real
GPU runs, with all broker data-integrity counters at zero.

## Deeper docs

- [Technical reference](docs/index.html) — architecture,
  topology and YAML model, broker per-slot loop, signal alignment, GPU compute,
  signal memory, multi-stream concurrency, profiling, performance, planned
  work. **Start here for design questions.**
- [OCUDU interop runbook](docs/ocudu-interop.md) — Docker gNB + srsUE attach
  procedure.
- [Distributed IQ over network](docs/distributed.md) — bandwidth, jitter, and
  packet-loss requirements when broker and radios run on different hosts.
- [Project structure](docs/project-structure.md) — local repo and remote RTX
  workstation layout.

## License

Released under the [MIT License](LICENSE). Copyright © 2026 Zhouyou Gu.
