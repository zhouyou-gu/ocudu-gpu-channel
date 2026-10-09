# topologies

Broker topology YAMLs (`--config`), grouped by purpose. File names are
unchanged from when they lived directly under `use_cases/`, so older logs and
docs still name them correctly.

| Folder | What | Main users |
|---|---|---|
| `basic/` | First runs: CPU backend (`local.cpu`), CUDA minimum (`mvp`, the Docker image default), the graph model (`graph`) | `scripts/remote/build-and-bench-cuda-mvp.sh`, `perf-sweep.sh`, `Dockerfile` |
| `channel_models/` | TR 38.901 TDL-A…E on one link; 2x2 MIMO with spatial correlation | `scripts/remote/perf-backend-compare.sh`, `gpu-test-sequence.sh` |
| `perf/` | Synthetic load, no radios: TDL-A fan-in 8, 16-edge stress | `scripts/remote/perf-fanin-sweep.sh`, `perf-sweep.sh`, `perf-deep-profile.sh` |
| `ocudu_docker/` | OCUDU gNB + srsUE interop: SISO base (also the base of several native gates), tx-offset, multi-UE, multi-gNB, rank-1 2x1/4x1 with 1–4 UEs | `scripts/remote/ocudu-*-smoke.sh`, `scripts/native/run-ocudu-{legacy-1x1,oai-1x1,multi-ue,multi-gnb}.sh` |
| `ocudu_native/` | Namespace-native fixtures: rank-1 2x1/4x1/4x1dl, oracle MRT, OAI 2x2 (+ unitary, near-singular), 2-port transport | `scripts/native/run-ocudu-{rank1-*,oai-2x2,mimo-2port-no-core}.sh` |
| `sionna/` | Sionna RT-driven: 1 gNB/1 UE, 2 gNB/2 UE, 4-port multi-gNB | `scripts/remote/ocudu-multi-gnb-smoke.sh`, `scripts/local/run_synthetic_web_ui.sh` |
| `cmx/` | CMX500/X310 hardware bridge: passthrough and downlink SNR sweeps | `tools/cmx-loop/` |

Not referenced by any script (kept as documented experiment fixtures):
`sionna/topology.sionna-1gnb-1ue`, `ocudu_native/topology.ocudu.oai-2x2-near-singular`,
`ocudu_native/topology.ocudu.rank1-4x1dl`, `ocudu_native/topology.ocudu.rank1-2x1-oracle-mrt`.
