# Project overview

(section-1)=

## Overview

ocudu-gpu-channel is a real-time, in-the-loop channel emulator that sits between live SDR stacks (OCUDU runtime, srsRAN gNB/UE) over ZMQ and applies CUDA channel models with a 5G NR slot-time budget. Deadline compliance requires measured qualification. This doc is the implementation reference — it assumes you have already read the [`README`](https://github.com/zhouyou-gu/ocudu-gpu-channel#readme) for positioning and quick-start.

**Single-antenna radios and multi-port radios.** Parts I–VI describe the engine on a graph of single-antenna radios, where one radio is one ZMQ endpoint pair and one edge is one scalar channel. That is the whole system for a 1×1 deployment and it is where a first reading should start. [Part VII](../concepts/radio-topology.md#part-vii) adds the overlay for radios with several antenna ports: several ports are grouped into one radio node, a link between two such radios carries an `Nt × Nr` matrix, and every coefficient of that matrix belongs to one sample epoch. There is no second engine underneath — a multi-port link expands into ordinary per-edge lanes, and a single-port topology is the `Nt = Nr = 1` case of the same path, byte for byte.

**Slot length and SCS.** The slot deadline depends on the subcarrier spacing: **1 ms at 15 kHz SCS**, **500 µs at 30 kHz SCS** (the bench default and the [§20](../reports/performance/measured-boundaries.md#perf) measurement frame), and 250 µs at 60 kHz SCS. Where this document says "1 ms slot" it refers to the 15 kHz case; the perf section and latency gate use the 30 kHz / 500 µs target. The bench is invoked with `--scs-khz N` and the green/yellow/red gate scales to the chosen N.

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

