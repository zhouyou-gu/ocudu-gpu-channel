# Applications

The C++ entrypoints here build the broker, benchmark, control client and synthetic IQ tools through the root CMake project.

| Component | Source entrypoint | Installed command |
|---|---|---|
| Sionna bridge | `python3 apps/sionna_bridge/run_bridge.py` | `ocudu-sionna-bridge` |
| Read-only dashboard | `python3 apps/dashboard/server.py` | `ocudu-dashboard` |

Install from the repository root with `python3 -m pip install '.[dashboard]'` or `python3 -m pip install '.[sionna]'`. The packages are `ocudu_dashboard` and `ocudu_sionna_bridge`; selecting the dashboard extra does not install Sionna. Both commands accept `--help` and the existing source CLI options.

Installed commands resolve relative input/output paths from the launch directory. The dashboard bundles its HTML and vendor modules; Sionna scenes and scenarios remain in [`use_cases/configs/`](../use_cases/configs/README.md). Supply absolute scene/scenario paths when launching outside the checkout. Source entrypoints preserve checkout-relative defaults.

The launchers [`run_web_ui.sh`](../scripts/local/run_web_ui.sh) and [`run_synthetic_web_ui.sh`](../scripts/local/run_synthetic_web_ui.sh) orchestrate the components. See the [integration guide](../docs/sionna-integration.md) for live setup and qualification limits.
