# Generated Sionna RT scenes

Each subdirectory is a Mitsuba scene that `run_bridge.py --scene <name>` loads
by name, alongside the scene names Sionna ships. A scenario config selects one
with its `"scene"` field.

| Scene | Geometry | Notes |
| --- | --- | --- |
| `sionna_simple_test` | Sionna built-in `simple_street_canyon` | Alias only — no files here. Six boxes and a floor that `--simple-road` splits into a road and two verges. |
| `sionna_SUTD_test` | OpenStreetMap, SUTD campus, Singapore | Built by `scripts/sionna_rt/build_osm_scene.py`. **See its `LICENSE.md`: the data is ODbL 1.0.** |

## Rebuilding an OpenStreetMap scene

```
python3 scripts/sionna_rt/build_osm_scene.py \
    --name sionna_SUTD_test --center 1.34174 103.96383 --half-extent-m 300
```

Each scene keeps the raw Overpass response in `osm.json`, so
`--offline --name <name>` rebuilds the meshes from it without touching the
network — use that when changing how geometry is generated, so the OSM
snapshot the scene was validated against does not move underneath you.

`manifest.json` records what every mesh is in scene metres: building ids, their
OSM ids and names, heights and how each height was derived, road centrelines,
and a ready-made `walk_ring_m` route around each building. Scenario configs are
written against those numbers.
