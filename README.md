# ocudu-gpu-channel

Project lead: **[Zhouyou Gu](https://github.com/zhouyou-gu)** (SUTD)<br>
Steering: **[Jihong Park](https://www.sutd.edu.sg/profile/park-jihong/)** (SUTD)<br>
Contributors: **[Minwoo Eun](https://github.com/MinwooEun)** (Yonsei University) · **[Hyunsoo Lee](https://github.com/ehs0)** (Yonsei University) · **Quanlong Zhao** (SUTD) ([contributions](README.md#contributors))

**GPU-accelerated, ZMQ-native channel emulator for live srsRAN and OCUDU stacks.**
Routes `cf32` IQ between radio endpoints and applies CUDA channel models
across multi-gNB, multi-UE and multi-antenna topologies. Processing targets
5G NR slot timing; deadline compliance depends on the workload.

## Get started

Read the [documentation](https://zhouyou-gu.github.io/ocudu-gpu-channel/) ([Markdown source](docs/README.md)). The recommended path is [current status](docs/getting_started/status.md) → [requirements](docs/getting_started/requirements.md) → [installation](docs/getting_started/installation.md) → [first synthetic CUDA run](docs/getting_started/first-run.md).

CUDA channel processing, matrix updates and multi-gNB telemetry are implemented. **Continuous moving two-UE traffic and strict zero-miss real-time qualification remain failed in the recorded live runs.** The status page separates code availability from measured outcomes and links the evidence.

## Explore

| Need | Documentation |
|---|---|
| Understand the engine and terminology | [Architecture](docs/concepts/architecture.md), [glossary](docs/concepts/glossary.md) |
| Configure or control a run | [Configuration](docs/reference/configuration.md), [CLI](docs/reference/cli.md), [control API](docs/reference/control-api.md) |
| Connect Sionna or inspect telemetry | [Sionna](docs/guides/sionna.md), [dashboard](docs/guides/dashboard.md) |
| Run a scenario | [Use-case catalog](docs/use_cases/README.md) |
| Develop or validate changes | [Project structure](docs/development/project-structure.md), [testing](docs/development/testing.md) |
| Evaluate measured results | [Reports](docs/reports/README.md), [historical designs](docs/history/README.md) |
| Compare related tools | [Project overview](docs/getting_started/overview.md#where-this-fits) |

## Contributors

This table is the project's authoritative contributor list. Original commits
and authorship are retained; contribution areas can overlap.

| Contributor | Affiliation | Role | Contribution areas |
|---|---|---|---|
| **[Zhouyou Gu](https://github.com/zhouyou-gu)** | Singapore University of Technology and Design (SUTD) | Project lead | Core broker and CPU/CUDA emulator, topology and runtime control, OCUDU/srsRAN interop, integration fixes and RTX validation, and multi-UE attachment/recovery work in the [srsRAN fork](https://github.com/zhouyou-gu/srsRAN_4G). |
| **[Jihong Park](https://www.sutd.edu.sg/profile/park-jihong/)** | Singapore University of Technology and Design (SUTD) | Steering | Project steering. |
| **[Minwoo Eun](https://github.com/MinwooEun)** | Yonsei University | Rank-1 MISO/SIMO contributor | Multi-port radios and physical-link state, fixed and correlated matrix channels, live 2×1/1×2 and 4×1/1×4 gates, wire-capture scoring and transport validation, and precoding/UE feasibility studies. |
| **[Hyunsoo Lee](https://github.com/ehs0)** | Yonsei University | Sionna integration contributor | Sionna RT bridge and live scalar/matrix updates, native integration, moving SUTD scene and antenna arrays, scene/ray/channel visualization, timing/resource views, and the initial gNB scheduler KPI panel. |
| **Quanlong Zhao** | Singapore University of Technology and Design (SUTD) | Research Assistant | Hardware-in-the-loop testing. |

The [integration guide](docs/history/milestones/sionna-integration-20260914.md#contribution-provenance)
records the normal merge and distinguishes the original Sionna work from
subsequent channel, dashboard and UE recovery fixes.

## License

Released under the [MIT License](LICENSE). Copyright © 2026 Zhouyou Gu.
