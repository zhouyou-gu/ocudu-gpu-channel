# Requirements

The primary operating path is a Linux host with an NVIDIA GPU, CUDA and ZeroMQ. Use the [platform guide](../guides/platforms.md) for the exact tested hardware, toolchains and source locks; a successful build alone does not qualify a platform.

| Component | Required for |
|---|---|
| C++20 compiler, CMake 3.22+, pkg-config and libzmq development files | Broker and synthetic tools |
| CUDA toolkit and compatible NVIDIA driver | CUDA backend; configure the architecture for the actual GPU |
| Python 3.10+ and the `dashboard` package extra | Dashboard only |
| Python environment with the `sionna` extra and its GPU requirements | Sionna ray tracing and bridge |
| Node.js (tested with 22.20.0) | Dashboard JavaScript regression checks |
| Python 3.12 with `docs/requirements.txt` | Documentation publishing only |
| Docker with NVIDIA Container Toolkit, or the provisioned native workspace | Corresponding live-stack workflow |

RTX 5090 validation uses the shared remote workspace. Keep credentials in ignored `.config`, copied from `.config.example`; remote helpers load it through Bash. Install missing remote tools in user space through the existing bootstrap scripts.

The CPU backend is a reference and development route. It does not require CUDA. The dashboard package does not require Sionna; scenario and scene inputs are supplied from the repository or explicit external paths.

Before a run, reserve the topology's ZMQ ports and the control, telemetry and HTTP ports you intend to use. Do not stop unrelated services to obtain them. The first-run example uses ports 2000, 2001, 2100 and 2101.

Next: [installation](installation.md).
