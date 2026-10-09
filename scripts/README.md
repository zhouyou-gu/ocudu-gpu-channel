# `scripts/`

Reproducible workflows around the broker. Application demos (robot fight,
scheduler benchmark) live in `use_cases/`, and their input configs live in
`use_cases/configs/`. This directory holds the workflows those demos and the
milestone gates build on.

| Directory | What it is for | Start here |
|---|---|---|
| `native/` | Rootless, Docker-free live gates on this host: OCUDU gNB ⇄ broker ⇄ srsUE/OAI UE with Open5GS. Workspace bootstrap and shared locks, config renderers (`render-*.py`), gate runners (`run-ocudu-*.sh`, each with an `-inner.sh` half that runs inside the namespace), and artifact checks (`verify-*.py`). `env.sh` and `oai-gate-defaults.sh` are shared with the `use_cases/` demo gates. | `native/README.md` |
| `cuda/` | The OCUDU CUDA-accelerated gNB: the current runners (`run-ocudu-cuda-*.sh`) and their helpers (`resolve-cuda-gnb.py`, `verify-*.py`, `summarize-*.py`, `with-cuda-mps.py`). Also the per-milestone scripts named after their milestone (`c0`–`c5`, `d5` in [CUDA_MILESTONES.md](../docs/history/milestones/CUDA_MILESTONES.md)), with source locks and gNB patches in `integrations/ocudu/`. | [CUDA_MILESTONES.md](../docs/history/milestones/CUDA_MILESTONES.md) |
| `cuda/jetson/` | Jetson AGX Orin counterparts (`j1`, `j2`) and the SCTP out-of-tree module prep. | [JETSON_MILESTONES.md](../docs/history/milestones/JETSON_MILESTONES.md) |
| `cuda/spark/` | DGX Spark containers and the `s2`–`s15` milestone scripts. | `cuda/spark/README.md`, [SPARK_MILESTONES.md](../docs/history/milestones/SPARK_MILESTONES.md) |
| `remote/` | Workflows that run on the RTX workstation over SSH and use `.config` through `common.sh`: toolchain bootstrap, GPU test sequence, Docker-based OCUDU smokes, perf sweeps. | `remote/README.md` |
| `local/` | Local launch orchestration: Sionna bridge + dashboard (`run_web_ui.sh`), synthetic dashboard loop and remote-smoke wrappers. | [Applications](../apps/README.md) |
| `figures/` | Regenerate committed figures: `regen_fading_figures.py` (`docs/assets/figures/`), `regen_perf_figures.py` (`docs/reports/performance/2026-05-sweeps/` from a perf `sweep.json`). | — |
| `tools/` | Standalone helpers: `gen_topology.py` (synthetic one-to-N / M-to-N topologies for the perf sweeps), `check_feed.py` (verify that the broker telemetry feed carries the expected links). | — |

Runtime code lives in [`apps/sionna_bridge/`](../apps/sionna_bridge) and [`apps/dashboard/`](../apps/dashboard). External-stack patches and revision pins live in [`integrations/`](../integrations/README.md).

Conventions:

- Shell scripts find their siblings through `script_dir` / `native_dir` and
  the repo through `project_root` / `repo_root`. Run them from any directory.
- Private host values (hostnames, users, IPs) stay in the ignored `.config`
  and are never written into tracked files.
