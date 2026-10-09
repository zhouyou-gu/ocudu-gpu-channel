# configs

Input configurations read by the demos, the native gates (`scripts/native/`)
and the remote / Docker smokes (`scripts/remote/`). Run outputs never land here.

| Folder | What |
|---|---|
| [`topologies/`](topologies/README.md) | Broker topology YAMLs (`--config`), grouped by purpose |
| [`sionna/`](sionna/README.md) | `scenarios/<scene>/` Sionna RT scenario JSONs; `scenes/` scene geometry. `scripts/sionna_rt/run_bridge.py` looks scenes up in `sionna/scenes/` by name, so that folder must stay put |
| `ran/ocudu/docker/` | OCUDU gNB configs for the Docker smokes. `gnb_zmq_b210_fdd_srsue.yaml` is also the base every native renderer rewrites |
| `ran/ocudu/native/` | OCUDU gNB fixtures for the native rank-1 / 2-port gates. Two names repeat `docker/`: same cell, native loopback endpoints |
| `ran/srsue/` | srsUE config templates (`.conf.in`) for the native legacy-1x1 and multi-UE renderers |
| `ran/oai/` | OAI nrUE config for the native OAI gates |
| `ran/open5gs/` | Open5GS subscriber CSVs for the native gates |
