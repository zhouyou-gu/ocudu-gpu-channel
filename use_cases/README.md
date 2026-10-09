# Use cases

Application demos, one folder each (code, gate script and README together),
plus `configs/`, the input configurations the demos and gates read.

| Demo | Gate (run from the repo root) |
|---|---|
| [robot_fight/](robot_fight/README.md) — sumo-robot arena over two cells, two brokers sharing one GPU | `bash use_cases/robot_fight/run-ocudu-robot-fight.sh` |
| [scheduler_benchmark/](scheduler_benchmark/README.md) — two OCUDU MAC schedulers under one seeded Sionna channel | `bash use_cases/scheduler_benchmark/run-ocudu-scheduler-benchmark.sh` |

The demo gates reuse the shared native tooling in `scripts/native/` (`env.sh`,
`oai-gate-defaults.sh`, workspace lock, base renderers). Run outputs land
under `$OCUDU_NATIVE_ROOT/{results,configs,data,run}/`, not here.

[`configs/`](configs/README.md) holds broker topologies, Sionna scenarios and
scenes, and the gNB / UE / core configs.
