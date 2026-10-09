# Licence and attribution for this scene

The geometry in this directory is derived from **OpenStreetMap** data:

> © OpenStreetMap contributors

OpenStreetMap data is made available under the **Open Database License
(ODbL) v1.0** — https://opendatacommons.org/licenses/odbl/1-0/

## What that means here

`osm.json` is a verbatim extract of the OpenStreetMap database, and the
`meshes/*.ply` files plus `manifest.json` are derived from it. Together they
are a **Derivative Database** under the ODbL, so this directory is offered
under the ODbL v1.0, separately from the licence covering the rest of this
repository. Redistributing it — modified or not — carries the same terms and
the same attribution.

The web UI displays the attribution on the scene panel whenever it loads a
scene whose `manifest.json` declares one, which is what the ODbL asks for when
the data is shown to someone. That notice comes from the `attribution` field
of `manifest.json`; do not remove it.

Sionna, this repository's code, and the ITU P.2040 material parameters are
**not** affected by the ODbL. Only the contents of this scene directory are.

## Source

- Extract: Overpass API, bounding box recorded in `osm.json` under `__scene__`
- Scene origin: see `origin_lat_lon` in `manifest.json`
- Generator: `scripts/sionna_rt/build_osm_scene.py`
