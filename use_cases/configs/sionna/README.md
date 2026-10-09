# Sionna RT scenario configs

A scenario is the dependency-free JSON contract shared by the launcher, the
bridge and the config renderers. `run_bridge.py --scenario-config <file>`
reads it, and the native launchers pass it through
`OCUDU_NATIVE_SIONNA_SCENARIO`.

Layout: `scenarios/<scene>/` holds the scenario JSONs, grouped by the scene
they name; `scenes/` holds the scene geometry. `run_bridge.py` resolves a
scenario's `scene` name against `scenes/` (or a Sionna built-in), so
`scenes/` must stay where it is; scenario files can move freely.

### `scenarios/simple_street/` — scene `sionna_simple_test` (Sionna built-in street canyon)

| File | Nodes | Links | Used by |
| --- | --- | --- | --- |
| `ocudu-docker.json` | 1 gNB + 1 UE, 1×1 | 2 | default for `scripts/native/run-ocudu-legacy-1x1.sh`; synthetic web UI |
| `ocudu-rank1.json` | 1 gNB (4T4R) + 1 car UE | 2 | `run-ocudu-external-rank1.sh`, `scripts/cuda/run-ocudu-cuda-sionna-rank1.sh` defaults |
| `ocudu-docker-multi-ue.json` | 1 gNB + 2 UEs | 4 | `scripts/remote/ocudu-multi-ue-smoke.sh`; web UI |
| `multi-gnb.json` | 2 gNB + 2 UE | 8 | `scripts/remote/ocudu-multi-gnb-smoke.sh`; web UI |
| `graph.json` | 1 gNB + 2 UE | 6 | web UI |

### `scenarios/sutd/` — scene `sionna_SUTD_test` (OSM campus, `scenes/sionna_SUTD_test/`)

| File | Nodes | Links | Used by |
| --- | --- | --- | --- |
| **`ocudu-rank1-sutd.json`** | 1 gNB (4T4R) + 1 car UE | 2 | **default for `run-ocudu-sionna-rank1.sh`** |
| `sionna-multi-ue-sutd.json` | 1 gNB + 2 UEs | 4 | default for `run-ocudu-sionna-multi-ue.sh` |
| `multi-gnb-sutd.json` | 2 gNB + 2 UE | 10 | web UI, `scripts/sionna_rt/measure_cell_coverage.py` |
| `sionna_SUTD_test.json` | 1 gNB + car UE + pedestrian UE | 5 | tests only |

### `scenarios/robot_ring/` — scene `robot_ring` (`scenes/robot_ring/`)

| File | Nodes | Links | Used by |
| --- | --- | --- | --- |
| `robot-ring.json` | 1 gNB + 2 robots | 4 | base ring; tests, `scenes/robot_ring/coverage_grid.py` |
| `robot-ring-walk.json` | 1 gNB + 2 robots | 4 | default for `use_cases/scheduler_benchmark` |
| `robot-ring-fight.json` | 2 gNB + 2 robots | 4 | default for `use_cases/robot_fight` |
| `robot-ring-crosstalk.json` | 1 gNB + 2 robots | 6 | tests only |
| `robot-ring-fight-shadow.json`, `-shadow-deep.json` | 2 gNB + 2 robots | 4 | not referenced (kept as fight variants) |

A gate starts a fixed number of gNB and UE processes, so a scenario with more
of either than its gate runs still traces fine through the bridge and the web
UI but cannot drive the live radio stacks; the renderers say so by name when
they refuse one.

## The 1 gNB / 1 UE example

`ocudu-rank1-sutd.json` is the reference single-cell setup:

- **gNB** — 4T4R, fixed, on the parapet of SUTD Building 2 at 30.5 m
  (roof 24.5 m plus a 6 m mast). At the roof *centre* the building's own roof
  cuts every path to the street, which is why the mast sits at the edge.
- **UE** — a car shuttling a 76 m stretch of the campus service road that runs
  along Building 2's north face, at 8.33 m/s (30 km/h), round trip 18.3 s.
- **Links** — one downlink and one uplink, both `sionna_rt`.
- **Scene** — `sionna_SUTD_test`, built from OpenStreetMap
  (see `scenes/sionna_SUTD_test/LICENSE.md`: the data is ODbL 1.0).

## Fields

| Key | Meaning |
| --- | --- |
| `scene` | scene XML path, a directory under `scenes/`, or a built-in Sionna scene |
| `simple_road` | `false` for a scene that ships its own ground and roads |
| `nodes.<id>.tx_array` / `rx_array` | `rows`×`cols`; the native renderer takes these as the source of truth for gNB and broker port counts |
| `nodes.<id>.start_m` | position in scene metres; optional when `route_m` is given |
| `nodes.<id>.route_m` + `route_mode` + `speed_mps` | a walked polyline, `pingpong` or `loop` |
| `nodes.<id>.velocity_mps` + `route_x_m` | the older constant-velocity form, still supported |
| `links[]` | `from`, `to`, `direction` (`downlink`/`uplink`/`crosstalk`), `model` |
| `solver` | `max_depth`, `samples_per_source`, `seed`, `path_polylines`, and a `propagation` block of the five mechanisms |

Anything the `solver` block omits keeps the command-line default, so a
scenario pins only what it means to pin. Which mechanisms are on decides how
many paths exist at all: with the default `los` + `specular_reflection` and
nothing else, the SUTD drop-off link carries 2 paths and the UE-to-UE link 11.
