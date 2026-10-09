# Project Structure

The local repository is the canonical Git source. The remote RTX workstation is a reproducible validation mirror for GPU builds, OCUDU integration, and benchmark runs.

## Repository responsibilities

| Location | Owns |
|---|---|
| `apps/` | Executable entrypoints, the Sionna bridge and dashboard with browser assets |
| `include/`, `src/` | C++ interfaces and broker / CPU / CUDA implementations |
| `integrations/` | External-stack patches, exact revision pins and the UHD–ZMQ bridge |
| `scripts/` | Workspace preparation, launch orchestration and validation |
| `use_cases/` | Scenario code, launchers, analysis and shared `configs/` |
| `tests/` | Correctness checks grouped by component |
| `benchmarks/` | Standalone performance measurements |
| `docs/` | Current guides, measured reports and development plans/history |
| `archive/` | Historical records |

External source checkouts, build outputs, logs, captures and datasets remain outside tracked source. Private workstation settings remain in ignored `.config`.

## Migration map — 2026-10-09

The migration was applied in separate commits. The destination paths below define the supported layout; no old-path wrappers are retained.

| Previous location | Destination |
|---|---|
| `examples/` | `use_cases/` (same internal hierarchy) |
| `scripts/sionna_rt/*.py`, `requirements.txt` | `apps/sionna_bridge/` |
| `scripts/sionna_rt/*.sh` | `scripts/local/` |
| `scripts/web_ui/` | `apps/dashboard/` |
| `scripts/native/patches/oai-*`, OAI patch lock | `integrations/oai/` |
| `scripts/native/patches/srsue-*`, srsUE patch lock | `integrations/srsran/` |
| Native OCUDU patches/patch lock; CUDA patches/source locks | `integrations/ocudu/` |
| `tools/usrp-zmq-bridge/` | `integrations/usrp/` |
| `tools/cmx-loop/`, `tools/README.md` | `use_cases/cmx500/` |
| `tests/test_*` | `tests/{core,sionna_bridge,dashboard,integrations,use_cases}/` |
| `tests/bench_mutation_cost.cu` | `benchmarks/bench_mutation_cost.cu` |

Shared workspace locks and platform profiles stay with the scripts that assemble multiple stacks. Patch bytes and source revisions remain unchanged. Repository paths change; executable names, flags, configuration schemas and wire protocols remain stable.

## Supported entrypoints

Run from the repository root unless an absolute path is supplied:

```sh
cmake -S . -B build -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON
cmake --build build
./build/ocudu-gpu-channel --config use_cases/configs/topologies/basic/topology.mvp.cuda.yaml

# Install only the runtime dependencies required by the selected component.
python3 -m pip install '.[dashboard]'
ocudu-dashboard --help
python3 -m pip install '.[sionna]'
ocudu-sionna-bridge --help

bash scripts/local/run_web_ui.sh --scenario /path/to/scenario.json --status-jsonl /path/to/status.jsonl --python /path/to/venv/bin/python
bash use_cases/robot_fight/run-ocudu-robot-fight.sh
bash use_cases/scheduler_benchmark/run-ocudu-scheduler-benchmark.sh
```

Direct source entrypoints remain available at `apps/sionna_bridge/run_bridge.py` and `apps/dashboard/server.py`. Installed commands accept the same flags. Supply scenario/scene paths explicitly when using the installed bridge outside the checkout; use-case assets are repository inputs, not bundled package data.

Validation runs on the RTX workstation: CTest for CPU and CUDA builds, Python tests, dashboard JavaScript checks and `scripts/remote/gpu-test-sequence.sh`. The dashboard package includes its HTML and vendored browser modules and does not require Sionna.

## Remote workspace

See [remote workspace operation](../guides/remote-workspace.md) for the workspace layout and helper commands.
